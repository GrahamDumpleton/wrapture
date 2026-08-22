"""OpenTelemetry export: wrapture events as OTel spans and metrics.

The sinks here turn the event stream into OpenTelemetry signals, so
the trees wrapture records also land in whatever OTLP backend is
listening. Requests become SERVER spans named access-log style
("GET /quote/widget"), the calls and blocks beneath them become
INTERNAL spans, and the captured arguments, results and annotate()
data ride along as span attributes. A second sink aggregates the
same events into metrics: the semantic-convention duration histogram
for requests, a per-path histogram for calls, and a counter of
operations begun.

The `sink` factory is one registration covering every signal, named
from config as `type = "wrapture.otel:sink"`. Its `signals` key says
which are enabled, shared facts like the service name sit at the top
of the one table, per-signal tuning nests beneath it, and an
`environment` table supplies defaults for OTel's own environment
variables, so the same config reaches an http/protobuf collector or
a gRPC one with the deployment environment always able to override
the file.

The OpenTelemetry dependencies are not part of base wrapture; the
`wrapture[otel]` extra installs them. The code ships in every wheel,
but nothing in wrapture imports this subpackage until a config or
the application asks for the sink, so a plain install pays nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import wrapture

from .environment import _apply_environment
from .metrics import OpenTelemetryMetricsSink
from .providers import _configure_meter_provider, _configure_provider
from .spans import OpenTelemetrySink

_SIGNALS = ("traces", "metrics")


def sink(
    *,
    service_name: str | None = None,
    signals: Sequence[str] = _SIGNALS,
    traces: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
) -> wrapture.Sink:
    """Build the OTel export sink: one registration for every signal.

    `signals` says which are enabled (both by default), and each has
    an optional table of its own tuning: `traces` takes the span
    sink's options plus `sample`, a keep rate applied to the trace
    export alone (the metrics beside it still hear every event), and
    `metrics` takes the metrics sink's options plus
    `export_interval`, seconds between metric exports.

    `environment` holds defaults for OTel's own environment
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


__all__ = ["OpenTelemetryMetricsSink", "OpenTelemetrySink", "sink"]
