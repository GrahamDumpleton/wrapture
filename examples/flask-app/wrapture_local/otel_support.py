"""An OpenTelemetry sink, entirely under the covers.

A stand-in for what a wrapture-otel package would ship: a sink that
turns wrapture events into OTel spans, so the request trees this demo
prints also land in whatever OTLP backend is listening. Requests
become SERVER spans named access-log style ("GET /quote/widget"),
the view handlers and helpers beneath them become INTERNAL spans, and
the captured arguments, results and annotate() data ride along as
span attributes.

The `sink` factory at the bottom is what wrapture-otel.toml names:
one registration covering every OTel signal. Its `signals` key says
which are enabled, traces (the span sink above, optionally sampled)
and metrics (a second sink that only counts and times, aggregating
the same events into a semconv duration histogram for requests, a
per-path histogram for calls, and a counter of operations begun).
Shared facts like the service name sit at the top of the one table,
per-signal tuning nests beneath it, and `[sink.environment]` supplies
defaults for OTel's own environment variables, so the same config
reaches an http/protobuf collector or a gRPC one with the deployment
environment always able to override the file.

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
from opentelemetry.metrics import get_meter, get_meter_provider, set_meter_provider
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue

import wrapture
from wrapture import Event

# Attribute values OTel accepts natively. Anything else is stringified.

_PRIMITIVES = (bool, int, float, str)

# The namespace wrapture-specific attribute names live under, for
# spans and metrics alike.

_PREFIX = "wrapture"

# Request data fields already exported under their semantic-convention
# names, so the close-time sweep does not repeat them as wrapture.data.*.

_SEMCONV_DATA = frozenset({"method", "path", "query"})


def _status_code(result: Any) -> int | None:
    # A WSGI status line is "200 OK"; keep just the number.

    if isinstance(result, str):
        first = result.split(" ", 1)[0]
        if first.isdigit():
            return int(first)
    return None


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
        kinds: Sequence[str] = ("call", "request", "block"),
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
            status = _status_code(event.result)
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

        self._sweep_data(span, event)
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

        self._sweep_data(span, event)
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
            method = event.data.get("method", "?")
            return f"{method} {event.data.get('path', '')}"

        return event.label or event.path

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


class OpenTelemetryMetricsSink(wrapture.Sink):
    """Aggregate wrapture events into OTel metrics.

    Where the span sink exports each event individually, this one only
    counts and times: request durations into the semantic-convention
    HTTP histogram attributed by method and status, call durations
    into a per-path histogram, and a counter of operations observed
    beginning. The set of bound paths is closed, chosen by the config,
    which is what makes the path a safe metric attribute; the raw
    request URL is unbounded, so requests are attributed by method and
    status only.

    Declaring "none" capture on both axes keeps the sink near-free: a
    metrics-only deployment records timings and outcomes without ever
    capturing a value.
    """

    capture_args = "none"
    capture_result = "none"

    def __init__(self, *, meter_name: str = "wrapture") -> None:
        meter = get_meter(meter_name)

        # The bucket boundaries are advisory: the semconv set for
        # requests, and a finer set for calls, whose durations sit
        # well below a network round trip.

        self._request_duration = meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="Duration of HTTP server requests.",
            explicit_bucket_boundaries_advisory=[
                0.005,
                0.01,
                0.025,
                0.05,
                0.075,
                0.1,
                0.25,
                0.5,
                0.75,
                1.0,
                2.5,
                5.0,
                7.5,
                10.0,
            ],
        )
        self._call_duration = meter.create_histogram(
            "wrapture.call.duration",
            unit="s",
            description="Duration of observed calls, by bound path.",
            explicit_bucket_boundaries_advisory=[
                0.0001,
                0.0005,
                0.001,
                0.005,
                0.01,
                0.05,
                0.1,
                0.5,
                1.0,
                5.0,
            ],
        )
        self._operations = meter.create_counter(
            "wrapture.operations",
            unit="{operation}",
            description="Operations observed beginning, by path and kind.",
        )

        self.skipped = 0

    # -- wrapture.Sink protocol -----------------------------------------

    def on_enter(self, event: Event) -> None:
        """Count the operation as it begins."""

        if event.kind not in ("call", "request"):
            self.skipped += 1
            return

        self._operations.add(
            1,
            {f"{_PREFIX}.path": event.path, f"{_PREFIX}.kind": event.kind},
        )

    def on_exit(self, event: Event) -> None:
        """Record the completed operation's duration."""

        self._record(event, error=None)

    def on_error(self, event: Event) -> None:
        """Record the failed operation's duration, attributed by the
        exception type."""

        exception = event.exception
        error = type(exception).__name__ if exception is not None else "error"
        self._record(event, error=error)

    def flush(self) -> None:
        """Push the current aggregation out through the exporter."""

        provider = get_meter_provider()
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            force_flush()

    # -- internals -------------------------------------------------------

    def _record(self, event: Event, error: str | None) -> None:
        if event.duration is None or event.kind not in ("call", "request"):
            return

        if event.kind == "request":
            attributes: dict[str, AttributeValue] = {}

            method = event.data.get("method")
            if method:
                attributes["http.request.method"] = str(method)
            status = _status_code(event.result)
            if status is not None:
                attributes["http.response.status_code"] = status
            if error is not None:
                attributes["error.type"] = error

            self._request_duration.record(event.duration, attributes)
            return

        attributes = {f"{_PREFIX}.path": event.path}
        if error is not None:
            attributes["error.type"] = error

        self._call_duration.record(event.duration, attributes)


_SIGNALS = ("traces", "metrics")


def sink(
    *,
    service_name: str | None = None,
    signals: Sequence[str] = _SIGNALS,
    traces: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
) -> wrapture.Sink:
    """Factory for the config's `type = "wrapture_local.otel_support:sink"`.

    One registration covers every OTel signal. `signals` says which
    are enabled (both by default), and each has an optional table of
    its own tuning: `[sink.traces]` takes the span sink's options
    plus `sample`, a keep rate applied to the trace export alone (the
    metrics beside it still hear every event), and `[sink.metrics]`
    takes the metrics sink's options plus `export_interval`, seconds
    between metric exports.

    `[sink.environment]` holds defaults for OTel's own environment
    variables: each key is uppercased, prefixed with OTEL_ when not
    already, and applied with setdefault, so a variable set in the
    real environment always wins. Named options like
    `export_interval` are passed to constructors explicitly and beat
    both. `service_name` names the service for every enabled signal.
    """

    if isinstance(signals, str):
        signals = [signals]
    unknown = sorted(set(signals) - set(_SIGNALS))
    if unknown or not signals:
        raise ValueError(f"signals must name some of {list(_SIGNALS)}, got {signals!r}")

    _apply_environment(environment or {})

    sinks: list[wrapture.Sink] = []

    # The trace export, optionally sampled inside this registration:
    # Sample decides per tree at the root, so the span sink beneath it
    # still sees whole, consistently paired trees, while the metrics
    # sink alongside hears everything.

    if "traces" in signals:
        options = dict(traces or {})
        sample = options.pop("sample", None)

        _configure_provider(service_name)

        span_sink: wrapture.Sink = OpenTelemetrySink(**options)
        if sample is not None:
            span_sink = wrapture.Sample(sample, span_sink)
        sinks.append(span_sink)

    if "metrics" in signals:
        options = dict(metrics or {})
        export_interval = options.pop("export_interval", None)

        if export_interval is not None and (
            not isinstance(export_interval, (int, float))
            or isinstance(export_interval, bool)
            or export_interval <= 0
        ):
            raise ValueError(
                f"export_interval must be a positive number of seconds,"
                f" got {export_interval!r}"
            )

        _configure_meter_provider(service_name, export_interval)
        sinks.append(OpenTelemetryMetricsSink(**options))

    # A lone signal is returned bare; several fan out. Either way the
    # capture declarations negotiate through: metrics alone stays at
    # "none", traces raise the fan-out to "summary".

    if len(sinks) == 1:
        return sinks[0]
    return wrapture.Fanout(*sinks)


def _apply_environment(environment: dict[str, Any]) -> None:
    """Apply `[sink.environment]` keys as OTel environment defaults.

    Mechanical mapping rather than named options: uppercase the key,
    prefix OTEL_ when missing, stringify the value, setdefault. The
    config file thereby supplies defaults for any of the SDK's
    documented variables, while a variable set in the real
    environment always wins.
    """

    for key, value in environment.items():
        name = key.upper()
        if not name.startswith("OTEL_"):
            name = f"OTEL_{name}"

        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)

        os.environ.setdefault(name, text)


def _configure_provider(service_name: str | None) -> None:
    """Stand up a TracerProvider from the standard OTel environment.

    OTEL_EXPORTER_OTLP_PROTOCOL picks the exporter (http/protobuf by
    default, or grpc), OTEL_EXPORTER_OTLP_ENDPOINT is read by the
    exporter itself, and OTEL_TRACES_EXPORTER=console swaps in the
    stdout exporter for a look without a collector. If the application
    already installed a provider, it is left alone.
    """

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
    elif _otlp_protocol("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter()
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter()

    provider = TracerProvider(resource=_resource(service_name))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def _configure_meter_provider(
    service_name: str | None, export_interval: float | None
) -> None:
    """Stand up a MeterProvider from the standard OTel environment.

    The metrics half of what `_configure_provider` does for traces:
    OTEL_EXPORTER_OTLP_PROTOCOL picks the exporter,
    OTEL_EXPORTER_OTLP_ENDPOINT is read by the exporter itself, and
    OTEL_METRICS_EXPORTER=console swaps in the stdout exporter. An
    application's own provider is left alone. `export_interval` is
    seconds between exports; left as None, the reader falls back to
    OTEL_METRIC_EXPORT_INTERVAL and then its 60 second default.
    """

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )

    if isinstance(get_meter_provider(), MeterProvider):
        return

    exporter: Any
    if os.environ.get("OTEL_METRICS_EXPORTER") == "console":
        exporter = ConsoleMetricExporter()
    elif _otlp_protocol("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL") == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )

        exporter = OTLPMetricExporter()
    else:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        exporter = OTLPMetricExporter()

    # An explicit interval given to the reader wins over the
    # environment variable, matching the SDK's own precedence.

    interval_millis = export_interval * 1000.0 if export_interval is not None else None
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=interval_millis
    )

    provider = MeterProvider(
        resource=_resource(service_name),
        metric_readers=[reader],
    )
    set_meter_provider(provider)


def _otlp_protocol(signal_variable: str) -> str:
    # The signal-specific protocol variable wins over the general one,
    # matching the OTel SDK's own precedence.

    return os.environ.get(
        signal_variable,
        os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )


def _resource(service_name: str | None) -> Any:
    from opentelemetry.sdk.resources import Resource

    return Resource.create({"service.name": service_name} if service_name else {})
