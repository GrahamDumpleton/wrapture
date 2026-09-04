"""The traces signal: one OTel span per wrapture event."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Sequence
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event as SpanEvent
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.sampling import DEFAULT_ON, Decision, Sampler
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    set_span_in_context,
)
from opentelemetry.util.types import AttributeValue

import wrapture
from wrapture import Event

from ..sinks import _exception_level
from ..trace import TraceSlot
from .common import (
    _CATEGORY_SEMCONV,
    _PRIMITIVES,
    _SEMCONV_DATA,
    _exception_attributes,
    _status_code,
)

_SAMPLED = TraceFlags(TraceFlags.SAMPLED)
_NOT_SAMPLED = TraceFlags(TraceFlags.DEFAULT)

_UNSET = Status(StatusCode.UNSET)
_ERROR = Status(StatusCode.ERROR)
_ABANDONED = Status(StatusCode.ERROR, "operation never completed")

_NO_LINKS: tuple[Link, ...] = ()


class _OpenSpan:
    """What the sink remembers about a span between its enter and its
    close: the identity it was given, its parent, when it started, and
    whether the tree is sampled at all. The event itself is kept so a
    span abandoned by its event can still be built by reap()."""

    __slots__ = ("context", "event", "links", "opened", "parent", "sampled", "start")

    def __init__(
        self,
        event: Event,
        context: SpanContext,
        parent: SpanContext | None,
        links: Sequence[Link],
        start: int,
        sampled: bool,
    ) -> None:
        self.event = event
        self.context = context
        self.parent = parent
        self.links = links
        self.start = start
        self.sampled = sampled
        self.opened = time.monotonic()


class OpenTelemetrySink(wrapture.Sink):
    """Emit one OTel span per wrapture event.

    The sink does not use the SDK's tracer. Everything a span needs is
    known when the event closes, so the sink builds the finished
    `ReadableSpan` there, in one step, and hands it to the span
    processor, the same object the SDK's own tracer hands spans to.
    The processor batches and exports exactly as it would for the
    tracer's spans; only the mutable `Span` the tracer would have
    kept open in between, with its validated attribute store and its
    locks, is skipped. What the sink records at enter is a small
    slotted entry: the ids, the parent, the start time.

    Spans are parented explicitly through `event.parent_id` rather
    than OTel's ambient context. That is deliberate: sink
    notifications arrive on the thread that ran the observed
    operation, but a generator or a streamed response body may begin
    on one thread and close on another, so the ambient context at
    close time is not reliably the one that was current at the start.
    wrapture's parent link is a process-wide sequence number that is
    correct in all of those cases. An event's links (`event.links`,
    the origins of work handed off with detach() or named by a
    consumer block) become the span's links, the causal-but-not-nested
    relationship OTel draws the same way.

    The sink also claims the tree's trace identity, per the TraceSlot
    contract, so that files, outbound headers and exported spans
    agree. The identity is the slot's: a trace id wrapture minted is
    exported as is, with the root span taking the minted span id as
    its own, and an identity that arrived in headers is continued,
    the root span getting a remote parent built from the slot. The
    sampler decides once per tree at the root, seeing the remote
    parent when there is one so an upstream decision is honoured, and
    children inherit the decision. As spans open and close, the
    slot's span-id register tracks the innermost exported span, so
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
        processor: SpanProcessor | None = None,
        resource: Resource | None = None,
        sampler: Sampler | None = None,
        tracer_name: str = "wrapture",
        kinds: Sequence[str] = ("call", "request", "block"),
        max_value_length: int = 512,
        attribute_prefix: str = "wrapture",
        exceptions: str = "full",
    ) -> None:
        # The export pipeline: a processor to hand finished spans to,
        # the resource and scope stamped on each. Left unspecified,
        # the processor is built from the standard OTel environment,
        # the way the [otel] table's factory does it.

        if processor is None:
            from .providers import _span_processor

            processor = _span_processor()

        self._processor = processor
        self._resource = resource if resource is not None else Resource.create({})
        self._sampler = sampler if sampler is not None else DEFAULT_ON
        self._scope = InstrumentationScope(tracer_name)

        self._kinds = frozenset(kinds)
        self._max_value_length = max_value_length
        self._exceptions = _exception_level(exceptions)

        # The attribute names this sink writes, formatted once.

        self._key_path = f"{attribute_prefix}.path"
        self._key_kind = f"{attribute_prefix}.kind"
        self._key_seq = f"{attribute_prefix}.seq"
        self._key_thread = f"{attribute_prefix}.thread_name"
        self._key_result = f"{attribute_prefix}.result"
        self._key_items = f"{attribute_prefix}.items"
        self._key_body_duration = f"{attribute_prefix}.body_duration"
        self._key_abandoned = f"{attribute_prefix}.abandoned"
        self._key_category = f"{attribute_prefix}.category"
        self._arg_prefix = f"{attribute_prefix}.arg."
        self._data_prefix = f"{attribute_prefix}.data."

        # Open spans by event.seq, so a close pairs with its start and
        # a child finds its parent.

        self._spans: dict[int, _OpenSpan] = {}
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
        # or reaped; the child then hangs off whatever the register
        # names rather than being lost.

        enclosing = None
        if event.parent_id is not None:
            with self._lock:
                enclosing = self._spans.get(event.parent_id)
            if enclosing is None:
                self.orphaned += 1

        links = self._links(event) if event.links else _NO_LINKS

        if enclosing is not None:
            trace_id = enclosing.context.trace_id
            span_id = random.getrandbits(64)
            parent: SpanContext | None = enclosing.context
            sampled = enclosing.sampled
        else:
            trace_id, span_id, parent, sampled = self._root(event, links)

        context = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=False,
            trace_flags=_SAMPLED if sampled else _NOT_SAMPLED,
        )

        # A child's span id goes into the register so outbound
        # injection parents downstream services onto it; the root's
        # was written by _root().

        if enclosing is not None:
            slot = self._slot(event)
            if slot is not None:
                slot.span_id = format(span_id, "016x")

        # The recording path stamps event.started just after on_enter
        # is delivered, so its bookkeeping is not charged to the
        # observed code; at this moment it is still None, and the
        # start must be taken from this sink's own pinned clock. Using
        # wall-clock now would put the start and the close-time end on
        # two different clocks, and their drift since the pinning
        # would make short spans come out negative.

        start = self._to_epoch_ns(event.started)
        if start is None:
            start = self._epoch_offset_ns + int(time.perf_counter() * 1e9)

        with self._lock:
            self._spans[event.seq] = _OpenSpan(
                event, context, parent, links, start, sampled
            )

    def on_exit(self, event: Event) -> None:
        """Close the event's span with its outcome and timing."""

        opened = self._take(event.seq)
        if opened is None:
            return

        if opened.sampled:
            self._export(opened, escaped=None, end=self._end_time(event))

        self._restore_register(event)

    def on_error(self, event: Event) -> None:
        """Close the event's span as failed, recording the exception
        that escaped and any noted against the event besides."""

        opened = self._take(event.seq)
        if opened is None:
            return

        if opened.sampled:
            self._export(opened, escaped=event.exception, end=self._end_time(event))

        self._restore_register(event)

    def flush(self) -> None:
        """Push batched spans to the exporter; called by wrapture at
        interpreter exit and from flush_sinks()."""

        self.reap()
        self._processor.force_flush()

    def on_fork(self) -> None:
        """Reset for a child process after os.fork(): a fresh lock,
        and the open-span table dropped, since the in-flight spans
        belong to the parent, which will close them. The SDK's own
        at-fork handling restarts the exporter worker thread."""

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
            stale = [
                seq for seq, opened in self._spans.items() if opened.opened < cutoff
            ]
            entries = [self._spans.pop(seq) for seq in stale]

        now = self._epoch_offset_ns + int(time.perf_counter() * 1e9)
        for opened in entries:
            if opened.sampled:
                self._export(opened, escaped=None, end=now, abandoned=True)

        return len(entries)

    @property
    def open_spans(self) -> int:
        """How many spans are currently open, awaiting their close."""

        with self._lock:
            return len(self._spans)

    # -- internals -------------------------------------------------------

    def _take(self, seq: int) -> _OpenSpan | None:
        with self._lock:
            return self._spans.pop(seq, None)

    def _slot(self, event: Event) -> TraceSlot | None:
        if event.trace is None:
            return None
        return event.trace.slots.get("w3c")

    def _root(
        self, event: Event, links: Sequence[Link]
    ) -> tuple[int, int, SpanContext | None, bool]:
        # A root's identity comes from the tree's slot, and the root
        # claims it. Three shapes: an identity that arrived in headers
        # is continued, the root taking the caller's span as a remote
        # parent; an identity wrapture minted is used as is, the root
        # taking the minted span id as its own; and a slot already
        # claimed (this event's parent was filtered or reaped) parents
        # onto whatever exported span the register names. A tree with
        # no slot at all, trace identity off, mints ids of its own.
        # The sampler runs here, once per tree, and sees the remote
        # parent when there is one.

        slot = self._slot(event)
        parent: SpanContext | None

        if slot is None:
            trace_id = random.getrandbits(128)
            span_id = random.getrandbits(64)
            sampled = self._sample(None, trace_id, event, links)
            return trace_id, span_id, None, sampled

        trace_id = int(slot.trace_id, 16)

        if slot.claimed:
            parent = SpanContext(
                trace_id=trace_id,
                span_id=int(slot.span_id, 16),
                is_remote=False,
                trace_flags=_SAMPLED if slot.sampled else _NOT_SAMPLED,
            )
            span_id = random.getrandbits(64)
            slot.span_id = format(span_id, "016x")
            return trace_id, span_id, parent, bool(slot.sampled)

        if slot.headers:
            parent = SpanContext(
                trace_id=trace_id,
                span_id=int(slot.span_id, 16),
                is_remote=True,
                trace_flags=_SAMPLED if slot.sampled else _NOT_SAMPLED,
            )
            span_id = random.getrandbits(64)
            sampled = self._sample(parent, trace_id, event, links)
            slot.span_id = format(span_id, "016x")
        else:
            parent = None
            span_id = int(slot.span_id, 16)
            sampled = self._sample(None, trace_id, event, links)

        slot.sampled = sampled
        slot.claimed = True

        return trace_id, span_id, parent, sampled

    def _sample(
        self,
        parent: SpanContext | None,
        trace_id: int,
        event: Event,
        links: Sequence[Link],
    ) -> bool:
        # The sampler's view: the parent as a context holding a
        # non-recording span, the way the SDK's tracer presents a
        # remote parent, so a parent-based sampler reads its flags.

        parent_context: Context | None = None
        if parent is not None:
            parent_context = set_span_in_context(NonRecordingSpan(parent))

        result = self._sampler.should_sample(
            parent_context,
            trace_id,
            self._name(event),
            self._kind(event),
            None,
            links or None,
        )

        return result.decision is Decision.RECORD_AND_SAMPLE

    def _links(self, event: Event) -> Sequence[Link]:
        # wrapture's event links map one to one onto span links: a
        # detached root (work handed to a thread, or a consumer block
        # naming the message it drained) carries the origin's ids,
        # captured from the register at the hand-off, so the link
        # lands on the span that was actually exported for the origin.
        # A link whose origin carried no trace identity has nothing a
        # span could point at and is left out.

        links: list[Link] = []

        for link in event.links:
            if link.trace_id is None or link.span_id is None:
                continue

            origin = SpanContext(
                trace_id=int(link.trace_id, 16),
                span_id=int(link.span_id, 16),
                is_remote=link.seq is None,
                trace_flags=_SAMPLED,
            )
            attributes = {
                name: self._coerce(value) for name, value in link.attributes.items()
            } or None
            links.append(Link(origin, attributes=attributes))

        return links or _NO_LINKS

    def _restore_register(self, event: Event) -> None:
        # A span just closed, so point the register back at the
        # enclosing exported span, keeping trace_headers() live for
        # whatever the operation's continuation sends next. At the
        # root's close nothing is enclosing and nothing is in flight,
        # so the register is left at the root's own id.

        if event.parent_id is None:
            return

        slot = self._slot(event)
        if slot is None or not slot.claimed:
            return

        with self._lock:
            enclosing = self._spans.get(event.parent_id)

        if enclosing is not None:
            slot.span_id = format(enclosing.context.span_id, "016x")

    def _export(
        self,
        opened: _OpenSpan,
        *,
        escaped: BaseException | None,
        end: int | None,
        abandoned: bool = False,
    ) -> None:
        # The one place a span is built: attributes, exception events
        # and status assembled from the closed event, then the
        # finished ReadableSpan handed to the processor.

        event = opened.event
        attributes, status = self._attributes(event)

        # Exceptions the code caught and noted against the event each
        # become an exception event on the span, placed at the moment
        # of the note, and the first of them sets the error status
        # unless a 5xx already did: the two agree rather than fight.
        # An exception that escaped is recorded last, at the close,
        # and names the error status whatever else was set.

        events: list[SpanEvent] = []

        for caught in event.caught:
            noted_at = self._to_epoch_ns(caught.at)
            events.append(self._exception_event(caught.exception, noted_at))
            if status is _UNSET:
                status = Status(StatusCode.ERROR, type(caught.exception).__name__)

        if escaped is not None:
            events.append(self._exception_event(escaped, end, escaped=True))
            status = Status(StatusCode.ERROR, type(escaped).__name__)

        if abandoned:
            attributes[self._key_abandoned] = True
            status = _ABANDONED

        span = ReadableSpan(
            name=self._name(event),
            context=opened.context,
            parent=opened.parent,
            resource=self._resource,
            attributes=attributes,
            events=events,
            links=opened.links,
            kind=self._kind(event),
            instrumentation_scope=self._scope,
            status=status,
            start_time=opened.start,
            end_time=end,
        )

        self._processor.on_end(span)

    def _exception_event(
        self,
        exception: BaseException,
        timestamp: int | None,
        *,
        escaped: bool = False,
    ) -> SpanEvent:
        # The semconv exception event, holding as much of the
        # exception as the `exceptions=` level allows.

        return SpanEvent(
            "exception",
            _exception_attributes(exception, self._exceptions, escaped=escaped),
            timestamp,
        )

    def _to_epoch_ns(self, perf_seconds: float | None) -> int | None:
        if perf_seconds is None:
            return None
        return self._epoch_offset_ns + int(perf_seconds * 1e9)

    def _end_time(self, event: Event) -> int | None:
        if event.started is None or event.duration is None:
            return None
        return self._to_epoch_ns(event.started + event.duration)

    def _kind(self, event: Event) -> SpanKind:
        # A request is the server side of an exchange, and so is a
        # "server" categorised event, the boundary the middlewares do
        # not speak for; the other categories are the client side of
        # one (or the producing and consuming sides, for a message or
        # a queued task); everything else is internal.

        if event.kind == "request" or event.category == "server":
            return SpanKind.SERVER

        if event.category in ("external", "database", "datastore"):
            return SpanKind.CLIENT
        if event.category in ("messaging", "task"):
            return SpanKind.PRODUCER
        if event.category == "consumer":
            return SpanKind.CONSUMER

        return SpanKind.INTERNAL

    def _name(self, event: Event) -> str:
        # Requests read access-log style, the way HTTP spans usually
        # do, named by the matched route pattern when the app
        # annotated one ("GET /quote/<item>", the low-cardinality form
        # backends group by) and by the path otherwise; everything
        # else uses the binding's friendly name.

        if event.kind == "request":
            route = event.data.get("route")
            return f"{self._method(event)} {route or event.data.get('path', '')}"

        # An RPC-shaped external call or server boundary (system and
        # operation both declared) reads the way OTel's RPC spans do,
        # named by the low-cardinality operation, service-qualified
        # when a service is declared; the patched location stays on
        # wrapture.path.

        if event.category in ("external", "server"):
            system = event.data.get("system")
            operation = event.data.get("operation")
            if system and operation:
                service = event.data.get("service")
                return f"{service}/{operation}" if service else str(operation)

        # A server boundary that is not RPC-shaped reads access-log
        # style like a request, when it declared a method.

        if event.category == "server" and event.data.get("method"):
            route = event.data.get("route")
            return f"{self._method(event)} {route or event.data.get('path', '')}"

        # A database or datastore span reads the way the database
        # conventions name a query: the low-cardinality operation,
        # qualified by the collection it acts on when the
        # instrumentation could supply one, else by the database. A
        # messaging, task or consumer span likewise reads as its
        # operation and destination. As for RPC, the contract name
        # takes precedence over a label, the patched location stays on
        # wrapture.path, and a span whose data names no operation
        # keeps the binding's friendly name.

        if event.category in ("database", "datastore"):
            operation = event.data.get("operation")
            if operation:
                target = event.data.get("collection") or event.data.get("database")
                return f"{operation} {target}" if target else str(operation)

        if event.category in ("messaging", "task", "consumer"):
            operation = event.data.get("operation")
            if operation:
                destination = event.data.get("destination")
                return f"{operation} {destination}" if destination else str(operation)

        return event.label or event.path

    def _method(self, event: Event) -> str:
        return str(event.data.get("method") or "?")

    def _attributes(self, event: Event) -> tuple[dict[str, AttributeValue], Status]:
        # Everything the event says about itself, read once at close:
        # annotate() merges into the data dict while the operation
        # runs, and a request gains fields (bytes, app_duration, the
        # matched route) as its body streams, so reading at enter
        # would only have to be repeated here.

        attributes: dict[str, AttributeValue] = {
            self._key_path: event.path,
            self._key_kind: event.kind,
            self._key_seq: event.seq,
            self._key_thread: event.thread_name,
        }
        status = _UNSET

        # A categorised event carries its category verbatim, whatever
        # its kind, so a backend that does not read the semantic
        # conventions can still select on it.

        if event.category is not None:
            attributes[self._key_category] = event.category

        # A request's descriptive fields live in event.data; map the
        # reserved ones onto their semantic-convention names. Its
        # result is its status line; a 5xx marks the span in error.

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
            route = event.data.get("route")
            if route:
                attributes["http.route"] = str(route)

            code = _status_code(event.result)
            if code is not None:
                attributes["http.response.status_code"] = code
                if code >= 500:
                    status = _ERROR

            for name, value in event.data.items():
                if name not in _SEMCONV_DATA:
                    attributes[self._data_prefix + name] = self._coerce(value)
        else:
            if event.arguments:
                for name, value in event.arguments.items():
                    attributes[self._arg_prefix + name] = self._coerce(value)

            if event.result is not wrapture.MISSING:
                attributes[self._key_result] = self._coerce(event.result)

            # The data keys of the category's contract map onto their
            # semantic-convention names; an external call's status of
            # 400 or above marks the span in error, as the HTTP client
            # conventions say. Everything else in data flattens as
            # usual.

            semconv = _CATEGORY_SEMCONV.get(event.category or "", {})

            for name, value in event.data.items():
                attribute = semconv.get(name)
                if attribute is None:
                    attributes[self._data_prefix + name] = self._coerce(value)
                elif attribute == "http.response.status_code":
                    code = _status_code(value) if isinstance(value, str) else value
                    if isinstance(code, int) and not isinstance(code, bool):
                        attributes[attribute] = code

                        # A client span is in error from 400, the
                        # HTTP client conventions; a server one only
                        # from 500, since a 4xx is the caller's fault.

                        threshold = 500 if event.category == "server" else 400
                        if code >= threshold:
                            status = _ERROR
                    else:
                        attributes[attribute] = self._coerce(value)
                else:
                    attributes[attribute] = self._coerce(value)

        # A streamed body carries two extra numbers worth keeping: how
        # many items it produced and the time spent producing them.

        if event.items is not None:
            attributes[self._key_items] = event.items
        if event.body_duration is not None:
            attributes[self._key_body_duration] = event.body_duration

        return attributes, status

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
