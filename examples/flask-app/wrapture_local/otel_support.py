"""An OpenTelemetry sink, entirely under the covers.

A stand-in for what a wrapture-otel package would ship: a sink that
turns wrapture events into OTel spans, so the request trees this demo
prints also land in whatever OTLP backend is listening. Requests
become SERVER spans named access-log style ("GET /quote/widget"),
the view handlers and helpers beneath them become INTERNAL spans, and
the captured arguments and results ride along as span attributes.

The `sink` factory at the bottom is what wrapture-otel.toml names. It
stands up a TracerProvider driven by the standard OTel environment
variables, so the same config reaches an http/protobuf collector or a
gRPC one depending on OTEL_EXPORTER_OTLP_PROTOCOL, with no changes
here or in the config file.

Requires opentelemetry-sdk and opentelemetry-exporter-otlp; see the
examples README for the run command.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue

import wrapture
from wrapture import Event

# Attribute values OTel accepts natively. Anything else is stringified.

_PRIMITIVES = (bool, int, float, str)


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
        kinds: Sequence[str] = ("call", "request"),
        max_value_length: int = 512,
        attribute_prefix: str = "wrapture",
    ) -> None:
        self._tracer = trace.get_tracer(tracer_name)
        self._kinds = frozenset(kinds)
        self._max_value_length = max_value_length
        self._prefix = attribute_prefix

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

        span = self._tracer.start_span(
            name=self._name(event),
            context=context,
            kind=SpanKind.SERVER if event.kind == "request" else SpanKind.INTERNAL,
            start_time=self._to_epoch_ns(event.started),
            attributes=self._enter_attributes(event),
            record_exception=False,
            set_status_on_exception=False,
        )

        with self._lock:
            self._spans[event.seq] = (span, time.monotonic())

    def on_exit(self, event: Event) -> None:
        """Close the event's span with its outcome and timing."""

        span = self._take(event.seq)
        if span is None:
            return

        # A request's result is its status line; report it the semconv
        # way and mark server errors. Anything else is just a result.

        if event.kind == "request":
            status = self._status_code(event.result)
            if status is not None:
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
        elif event.result is not wrapture.MISSING:
            span.set_attribute(f"{self._prefix}.result", self._coerce(event.result))

        # A streamed body carries two extra numbers worth keeping: how
        # many items it produced and the time spent producing them.

        if event.items is not None:
            span.set_attribute(f"{self._prefix}.items", event.items)
        if event.body_duration is not None:
            span.set_attribute(f"{self._prefix}.body_duration", event.body_duration)

        span.end(end_time=self._end_time(event))

    def on_error(self, event: Event) -> None:
        """Close the event's span as failed, recording the exception."""

        span = self._take(event.seq)
        if span is None:
            return

        if event.exception is not None:
            span.record_exception(event.exception)
            span.set_status(Status(StatusCode.ERROR, type(event.exception).__name__))
        else:
            span.set_status(Status(StatusCode.ERROR))

        span.end(end_time=self._end_time(event))

    def flush(self) -> None:
        """Push batched spans to the exporter; called by wrapture at
        interpreter exit and from flush_sinks()."""

        self.reap()

        provider = trace.get_tracer_provider()
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            force_flush()

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

    def _to_epoch_ns(self, perf_seconds: float | None) -> int | None:
        if perf_seconds is None:
            return None
        return self._epoch_offset_ns + int(perf_seconds * 1e9)

    def _end_time(self, event: Event) -> int | None:
        if event.started is None or event.duration is None:
            return None
        return self._to_epoch_ns(event.started + event.duration)

    def _name(self, event: Event) -> str:
        # Requests read access-log style, the way HTTP spans usually
        # do; everything else uses the binding's friendly name.

        if event.kind == "request":
            method = event.data.get("method", "?")
            return f"{method} {event.data.get('path', '')}"

        return event.label or event.path

    def _status_code(self, result: Any) -> int | None:
        # A WSGI status line is "200 OK"; keep just the number.

        if isinstance(result, str):
            first = result.split(" ", 1)[0]
            if first.isdigit():
                return int(first)
        return None

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


def sink(**options: Any) -> OpenTelemetrySink:
    """Factory for the config's `type = "wrapture_local.otel_support:sink"`.

    Every other key on the `[[sink]]` table arrives here as a keyword
    argument; `service_name` configures the provider and the rest pass
    through to the sink itself.
    """

    service_name = options.pop("service_name", None)
    _configure_provider(service_name)

    return OpenTelemetrySink(**options)


def _configure_provider(service_name: str | None) -> None:
    """Stand up a TracerProvider from the standard OTel environment.

    OTEL_EXPORTER_OTLP_PROTOCOL picks the exporter (http/protobuf by
    default, or grpc), OTEL_EXPORTER_OTLP_ENDPOINT is read by the
    exporter itself, and OTEL_TRACES_EXPORTER=console swaps in the
    stdout exporter for a look without a collector. If the application
    already installed a provider, it is left alone.
    """

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return

    # Pick the exporter the environment asks for. The OTLP exporters
    # read OTEL_EXPORTER_OTLP_ENDPOINT and its friends themselves, so
    # only the protocol choice happens here.

    exporter: Any
    if os.environ.get("OTEL_TRACES_EXPORTER") == "console":
        exporter = ConsoleSpanExporter()
    else:
        protocol = os.environ.get(
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
            os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
        )

        if protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

        exporter = OTLPSpanExporter()

    resource = Resource.create({"service.name": service_name} if service_name else {})

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
