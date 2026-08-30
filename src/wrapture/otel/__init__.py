"""OpenTelemetry export: wrapture events as spans, metrics and logs.

The sinks here turn the event stream into OpenTelemetry signals, so
the trees wrapture records also land in whatever OTLP backend is
listening. Requests become SERVER spans named access-log style
("GET /quote/widget"), the calls and blocks beneath them become
INTERNAL spans, and the captured arguments, results and annotate()
data ride along as span attributes. A second sink aggregates the
same events into metrics: the semantic-convention duration histogram
for requests, a per-path histogram for calls, and a counter of
operations begun. A third exports the log events the [[log]]
captures select, through the OTel logs bridge, each record
correlated to the exported span it happened inside.

The `sink` factory is one registration covering every signal. In
config it is the top-level `[otel]` table, whose keys are the
factory's arguments and whose sink always registers ahead of the
`[[sink]]` list; in code, call `sink()` and register the result
before other sinks. The `signals` key says which are enabled, shared
facts like the service name sit at the top of the one table,
per-signal tuning nests beneath it, and an `environment` table
supplies defaults for OTel's own environment variables, so the same
config reaches an http/protobuf collector or a gRPC one with the
deployment environment always able to override the file.

The export pipelines are wrapture's own: the factory builds them
from the table and the environment and hands them to the sinks
directly, without installing or consulting the SDK's global
providers. The span sink in particular does not go through the
SDK's tracer at all; it builds each finished span itself and hands
it to the span processor, which is where the cost of exporting a
span is kept to the export.

The OpenTelemetry dependencies are not part of base wrapture; the
`wrapture[otel]` extra installs them. The code ships in every wheel,
but nothing in wrapture imports this subpackage until a config or
the application asks for the sink, so a plain install pays nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import wrapture
from wrapture.sinks import _exception_level

from . import providers
from .environment import _apply_environment
from .logs import OpenTelemetryLogsSink
from .metrics import OpenTelemetryMetricsSink
from .spans import OpenTelemetrySink

_SIGNALS = ("traces", "metrics", "logs")


def sink(
    *,
    service_name: str | None = None,
    signals: Sequence[str] = _SIGNALS,
    traces: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    logs: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    exceptions: str = "full",
) -> wrapture.Sink:
    """Build the OTel export sink: one registration for every signal.

    `signals` says which are enabled (all by default), and each has
    an optional table of its own tuning: `traces` takes the span
    sink's options plus `sample`, a keep rate applied to the trace
    export alone (the metrics beside it still hear every event), and
    `sampler` with `sampler_arg`, the OTel sampler by its documented
    name ("parentbased_traceidratio" with a keep rate, say), the
    config spelling of OTEL_TRACES_SAMPLER that reaches only
    wrapture's own pipeline; `metrics` takes the metrics sink's
    options plus `export_interval`, seconds between metric exports;
    and `logs` takes the logs sink's options.

    `environment` holds defaults for OTel's own environment
    variables: each key is uppercased, prefixed with OTEL_ when not
    already, and applied with setdefault, so a variable set in the
    real environment always wins. The variables are process-wide,
    visible to any other OTel setup in the application too. Named
    options like `export_interval` and `sampler` are passed to
    constructors explicitly, beat both, and touch nothing shared.
    `service_name` names the service for every enabled signal.

    `exceptions` says how much of an exception the traces and logs
    signals export: "full" (type, message and stacktrace, the
    default), "message" (no stacktrace) or "type" (the type name
    alone), for deployments where exception messages may carry
    values a backend should not see. One setting for both signals,
    so it cannot be set on one and forgotten on the other.
    """

    exceptions = _exception_level(exceptions)

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
    # sink alongside hears everything. The bare span sink is kept for
    # the logs sink below, whose records correlate to open spans
    # through its table.

    spans: OpenTelemetrySink | None = None
    if "traces" in signals:
        options = dict(traces or {})
        sample = options.pop("sample", None)
        sampler_name = options.pop("sampler", None)
        sampler_arg = options.pop("sampler_arg", None)

        if sampler_name is not None and not isinstance(sampler_name, str):
            raise ValueError(f"sampler must be a sampler name, got {sampler_name!r}")
        if sampler_arg is not None and (
            not isinstance(sampler_arg, (int, float)) or isinstance(sampler_arg, bool)
        ):
            raise ValueError(f"sampler_arg must be a number, got {sampler_arg!r}")

        processor, resource, sampler = providers._trace_pipeline(
            service_name, sampler_name, sampler_arg
        )

        spans = OpenTelemetrySink(
            processor=processor,
            resource=resource,
            sampler=sampler,
            exceptions=exceptions,
            **options,
        )
        span_sink: wrapture.Sink = spans
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

        provider = providers._meter_provider(service_name, export_interval)
        sinks.append(OpenTelemetryMetricsSink(provider=provider, **options))

    if "logs" in signals:
        options = dict(logs or {})

        provider = providers._logger_provider(service_name)
        sinks.append(
            OpenTelemetryLogsSink(
                provider=provider, spans=spans, exceptions=exceptions, **options
            )
        )

    # A lone signal is returned bare; several fan out. Either way the
    # capture declarations negotiate through: metrics and logs alone
    # stay at "none", traces raise the fan-out to "summary".

    if len(sinks) == 1:
        return sinks[0]
    return wrapture.Fanout(*sinks)


__all__ = [
    "OpenTelemetryLogsSink",
    "OpenTelemetryMetricsSink",
    "OpenTelemetrySink",
    "sink",
]
