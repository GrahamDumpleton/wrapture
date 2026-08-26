"""The traces signal: one OTel span per wrapture event."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
)
from opentelemetry.util.types import AttributeValue

import wrapture
from wrapture import Event

from ..sinks import _exception_level
from ..trace import TraceSlot
from .common import _PRIMITIVES, _SEMCONV_DATA, _exception_attributes, _status_code


class OpenTelemetrySink(wrapture.Sink):
    """Emit one OTel span per wrapture event.

    Spans are parented explicitly through `event.parent_id` rather than
    OTel's ambient context. That is deliberate: sink notifications
    arrive on the thread that ran the observed operation, but a
    generator or a streamed response body may begin on one thread and
    close on another, so the ambient context at close time is not
    reliably the one that was current at the start. wrapture's parent
    link is a process-wide sequence number that is correct in all of
    those cases.

    The sink also claims the tree's trace identity, writing into the
    w3c slot per the TraceSlot contract so that files, outbound
    headers and exported spans agree. An identity that arrived in
    headers is continued: the root span gets a remote parent built
    from the slot, and only the span-id register and the claimed flag
    are written. An identity wrapture minted locally is replaced
    wholesale at the root event's delivery, before the operation's
    body runs: the SDK generates its own trace id, and the slot takes
    it, so the backend shows a clean native root. As spans open and
    close, the register tracks the innermost exported span, so
    `trace_headers()` carries a live parent at any moment inside the
    tree.
    """

    # "summary" is the right declaration for an exporting sink: enough
    # to put bounded values on a span, without forcing every binding in
    # the process to retain live objects the way "reference" would.

    capture_args = "summary"
    capture_result = "summary"

    def __init__(
        self,
        *,
        tracer_name: str = "wrapture",
        kinds: Sequence[str] = ("call", "request", "block"),
        max_value_length: int = 512,
        attribute_prefix: str = "wrapture",
        exceptions: str = "full",
    ) -> None:
        self._tracer = trace.get_tracer(tracer_name)
        self._kinds = frozenset(kinds)
        self._max_value_length = max_value_length
        self._prefix = attribute_prefix
        self._exceptions = _exception_level(exceptions)

        # Open spans by event.seq, so a close pairs with its start and
        # a child finds its parent. The wall-clock entry lets reap()
        # end spans whose events never close.

        self._spans: dict[int, tuple[trace.Span, float]] = {}
        self._lock = threading.Lock()

        # wrapture stamps events on the perf_counter clock; OTel wants
        # absolute epoch nanoseconds. Pin the two together once, here,
        # sampling both clocks in the same breath.

        self._epoch_offset_ns = time.time_ns() - int(time.perf_counter() * 1e9)

        # Honest counters, for anyone wondering where a span went.

        self.skipped = 0
        self.orphaned = 0

    # -- wrapture.Sink protocol -----------------------------------------

    def on_enter(self, event: Event) -> None:
        """Open a span for the event, parented on the enclosing one."""

        if event.kind not in self._kinds:
            self.skipped += 1
            return

        # Parent explicitly. A missing parent means it was filtered out
        # or reaped; the child becomes a root rather than being lost.

        context = None
        if event.parent_id is not None:
            with self._lock:
                entry = self._spans.get(event.parent_id)
            if entry is not None:
                context = trace.set_span_in_context(entry[0])
            else:
                self.orphaned += 1

        # A root whose identity arrived in headers continues the
        # caller's trace: the span gets a remote parent built from the
        # slot, with the sampled flag riding along for the parent-based
        # sampler to honour, instead of starting a detached trace.

        slot = self._slot(event)
        if context is None and slot is not None and slot.headers and not slot.claimed:
            context = self._remote_parent(slot)

        # The recording path stamps event.started just after on_enter
        # is delivered, so its bookkeeping is not charged to the
        # observed code; at this moment it is still None, and the
        # start must be taken from this sink's own pinned clock. The
        # SDK's fallback (wall-clock now) would put the start and the
        # close-time end on two different clocks, and their drift
        # since the pinning would make short spans come out negative.

        start_time = self._to_epoch_ns(event.started)
        if start_time is None:
            start_time = self._epoch_offset_ns + int(time.perf_counter() * 1e9)

        span = self._tracer.start_span(
            name=self._name(event),
            context=context,
            kind=SpanKind.SERVER if event.kind == "request" else SpanKind.INTERNAL,
            start_time=start_time,
            attributes=self._enter_attributes(event),
            record_exception=False,
            set_status_on_exception=False,
        )

        if slot is not None:
            self._claim(slot, span)

        with self._lock:
            self._spans[event.seq] = (span, time.monotonic())

    def on_exit(self, event: Event) -> None:
        """Close the event's span with its outcome and timing."""

        span = self._take(event.seq)
        if span is None:
            return

        # A request's result is its status line; report it the semconv
        # way and mark server errors. Anything else is just a result.

        errored = False

        if event.kind == "request":
            status = _status_code(event.result)
            if status is not None:
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                    errored = True
        elif event.result is not wrapture.MISSING:
            span.set_attribute(f"{self._prefix}.result", self._coerce(event.result))

        # Routing matches after the span opened, so a route annotation
        # (the matched pattern, "/quote/<item>") is only known now:
        # export it under its semconv name and rename the span to the
        # low-cardinality "METHOD route" form backends group by. A
        # request that matched no route keeps its path-based name.

        if event.kind == "request":
            route = event.data.get("route")
            if route:
                span.set_attribute("http.route", str(route))
                span.update_name(f"{self._method(event)} {route}")

        # A streamed body carries two extra numbers worth keeping: how
        # many items it produced and the time spent producing them.

        if event.items is not None:
            span.set_attribute(f"{self._prefix}.items", event.items)
        if event.body_duration is not None:
            span.set_attribute(f"{self._prefix}.body_duration", event.body_duration)

        # Exceptions the code caught and noted against the event each
        # become an exception event on the span, placed at the moment
        # of the note, and the first of them sets the error status
        # unless a 5xx already did: the two agree rather than fight.

        self._record_caught(span, event, errored=errored)

        self._sweep_data(span, event)
        span.end(end_time=self._end_time(event))
        self._restore_register(event)

    def on_error(self, event: Event) -> None:
        """Close the event's span as failed, recording the exception
        that escaped and any noted against the event besides."""

        span = self._take(event.seq)
        if span is None:
            return

        if event.exception is not None:
            self._record_exception(span, event.exception)
            span.set_status(Status(StatusCode.ERROR, type(event.exception).__name__))
        else:
            span.set_status(Status(StatusCode.ERROR))

        self._record_caught(span, event, errored=True)

        self._sweep_data(span, event)
        span.end(end_time=self._end_time(event))
        self._restore_register(event)

    def _record_caught(self, span: trace.Span, event: Event, *, errored: bool) -> None:
        # One exception event per noted exception, timestamped from the
        # note's perf_counter moment through the sink's pinned clock so
        # it sits inside the span on the same clock as its start and
        # end. The status is set to ERROR once, from the first note,
        # and left alone when the span is already in error.

        for caught in event.caught:
            self._record_exception(
                span, caught.exception, timestamp=self._to_epoch_ns(caught.at)
            )

            if not errored:
                span.set_status(
                    Status(StatusCode.ERROR, type(caught.exception).__name__)
                )
                errored = True

    def _record_exception(
        self,
        span: trace.Span,
        exception: BaseException,
        *,
        timestamp: int | None = None,
    ) -> None:
        # At "full" the SDK records type, message and stacktrace; the
        # reduced levels add the exception event by hand with only the
        # attributes the level allows.

        if self._exceptions == "full":
            span.record_exception(exception, timestamp=timestamp)
        else:
            span.add_event(
                "exception",
                attributes=_exception_attributes(exception, self._exceptions),
                timestamp=timestamp,
            )

    def flush(self) -> None:
        """Push batched spans to the exporter; called by wrapture at
        interpreter exit and from flush_sinks()."""

        self.reap()

        provider = trace.get_tracer_provider()
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            force_flush()

    def on_fork(self) -> None:
        """Reset for a child process after os.fork(): a fresh lock,
        and the open-span table dropped, since the in-flight spans
        belong to the parent, which will close them. The SDK's own
        at-fork handling restarts the exporter worker threads."""

        self._lock = threading.Lock()
        self._spans = {}

    # -- housekeeping ----------------------------------------------------

    def reap(self, max_age: float = 300.0) -> int:
        """End spans whose events never closed.

        An event that never closes, such as a response body abandoned
        mid-iteration, gets an enter and no exit. Ending its span with
        an explicit status after `max_age` seconds is more useful than
        holding it forever and exporting nothing.
        """

        cutoff = time.monotonic() - max_age
        with self._lock:
            stale = [seq for seq, (_, opened) in self._spans.items() if opened < cutoff]
            entries = [self._spans.pop(seq) for seq in stale]

        for span, _ in entries:
            span.set_status(Status(StatusCode.ERROR, "operation never completed"))
            span.set_attribute(f"{self._prefix}.abandoned", True)
            span.end()

        return len(entries)

    @property
    def open_spans(self) -> int:
        """How many spans are currently open, awaiting their close."""

        with self._lock:
            return len(self._spans)

    # -- internals -------------------------------------------------------

    def _take(self, seq: int) -> trace.Span | None:
        with self._lock:
            entry = self._spans.pop(seq, None)
        return entry[0] if entry is not None else None

    def _slot(self, event: Event) -> TraceSlot | None:
        if event.trace is None:
            return None
        return event.trace.slots.get("w3c")

    def _remote_parent(self, slot: TraceSlot) -> Context:
        # The caller's identity as a remote parent SpanContext, so the
        # exported root continues the upstream trace and the sampler
        # sees the upstream sampling decision.

        sampled = TraceFlags.SAMPLED if slot.sampled else TraceFlags.DEFAULT
        parent = SpanContext(
            trace_id=int(slot.trace_id, 16),
            span_id=int(slot.span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(sampled),
        )

        return trace.set_span_in_context(NonRecordingSpan(parent))

    def _claim(self, slot: TraceSlot, span: trace.Span) -> None:
        # Write the exported span into the slot, per the TraceSlot
        # contract: the register takes the new span's id so outbound
        # injection parents downstream services onto a span that
        # really got exported. A minted identity is replaced wholesale
        # at its first (root) claim, trace id and all, so files,
        # headers and spans agree on the SDK's id; an identity that
        # arrived in headers keeps its trace id, which the remote
        # parenting above already made the span's own.

        span_context = span.get_span_context()

        if not slot.claimed and not slot.headers:
            slot.trace_id = format(span_context.trace_id, "032x")
            slot.sampled = bool(span_context.trace_flags.sampled)

        slot.span_id = format(span_context.span_id, "016x")
        slot.claimed = True

    def _restore_register(self, event: Event) -> None:
        # A span just closed, so point the register back at the
        # enclosing exported span, keeping trace_headers() live for
        # whatever the operation's continuation sends next. At the
        # root's close nothing is enclosing and nothing is in flight,
        # so the register is left at the root's own id.

        slot = self._slot(event)
        if slot is None or not slot.claimed or event.parent_id is None:
            return

        with self._lock:
            entry = self._spans.get(event.parent_id)

        if entry is not None:
            parent_context = entry[0].get_span_context()
            slot.span_id = format(parent_context.span_id, "016x")

    def _to_epoch_ns(self, perf_seconds: float | None) -> int | None:
        if perf_seconds is None:
            return None
        return self._epoch_offset_ns + int(perf_seconds * 1e9)

    def _end_time(self, event: Event) -> int | None:
        if event.started is None or event.duration is None:
            return None
        return self._to_epoch_ns(event.started + event.duration)

    def _sweep_data(self, span: trace.Span, event: Event) -> None:
        # annotate() merges into the in-flight event's data dict, after
        # on_enter has already read it, and a request gains fields
        # (bytes, app_duration) as its body streams, so the dict is
        # swept again at close. Re-setting a key exported at enter is
        # fine: the last write before end() wins.

        skip = _SEMCONV_DATA if event.kind == "request" else frozenset()

        for name, value in event.data.items():
            if name not in skip:
                span.set_attribute(f"{self._prefix}.data.{name}", self._coerce(value))

    def _name(self, event: Event) -> str:
        # Requests read access-log style, the way HTTP spans usually
        # do; everything else uses the binding's friendly name.

        if event.kind == "request":
            return f"{self._method(event)} {event.data.get('path', '')}"

        return event.label or event.path

    def _method(self, event: Event) -> str:
        return str(event.data.get("method") or "?")

    def _enter_attributes(self, event: Event) -> dict[str, AttributeValue]:
        attributes: dict[str, AttributeValue] = {
            f"{self._prefix}.path": event.path,
            f"{self._prefix}.kind": event.kind,
            f"{self._prefix}.seq": event.seq,
            f"{self._prefix}.thread_name": event.thread_name,
        }

        # A request's descriptive fields live in event.data; map the
        # obvious ones onto their semantic-convention names.

        if event.kind == "request":
            method = event.data.get("method")
            if method:
                attributes["http.request.method"] = str(method)
            path = event.data.get("path")
            if path is not None:
                attributes["url.path"] = str(path)
            query = event.data.get("query")
            if query:
                attributes["url.query"] = str(query)
            return attributes

        if event.arguments:
            for name, value in event.arguments.items():
                attributes[f"{self._prefix}.arg.{name}"] = self._coerce(value)

        if event.data:
            for name, value in event.data.items():
                attributes[f"{self._prefix}.data.{name}"] = self._coerce(value)

        return attributes

    def _coerce(self, value: Any) -> AttributeValue:
        # Values arriving here are already bounded summaries, because
        # the sink declares "summary" capture, but another sink in the
        # process can raise the effective level, so a live object can
        # still arrive. Hence the defensive repr.

        if isinstance(value, _PRIMITIVES):
            if isinstance(value, str) and len(value) > self._max_value_length:
                return value[: self._max_value_length] + "..."
            return value

        try:
            text = repr(value)
        except Exception:
            return f"<unrepresentable {type(value).__name__}>"

        if len(text) > self._max_value_length:
            text = text[: self._max_value_length] + "..."
        return text
