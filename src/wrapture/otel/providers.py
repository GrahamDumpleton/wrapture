"""SDK provider setup: standing up tracer and meter providers from
config and the standard OTel environment.

The posture is wrapture-first: choosing wrapture means taking all it
does, including standing up the SDK providers, which is what the
zero-code story requires, since in the config-driven case there is
no application code to configure the SDK. A provider the application
already installed wins as the failsafe, with a warning naming what
is lost by deferring to it.
"""

from __future__ import annotations

import os
import warnings
from typing import Any

from opentelemetry import trace
from opentelemetry.metrics import get_meter_provider, set_meter_provider

import wrapture

from .environment import _otlp_protocol


def _configure_provider(service_name: str | None) -> None:
    """Stand up a TracerProvider from the standard OTel environment.

    OTEL_EXPORTER_OTLP_PROTOCOL picks the exporter (http/protobuf by
    default, or grpc), OTEL_EXPORTER_OTLP_ENDPOINT is read by the
    exporter itself, and OTEL_TRACES_EXPORTER=console swaps in the
    stdout exporter for a look without a collector. A provider the
    application already installed wins as the failsafe, with a
    warning naming what is lost.

    The provider's sampler is the SDK default, parentbased_always_on,
    so an upstream "do not sample" decision carried in a remote
    parent is honoured; the standard OTEL_TRACES_SAMPLER variables,
    in the environment table or the real environment, override it.
    """

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        warnings.warn(
            "an OpenTelemetry tracer provider is already configured, so"
            " wrapture's traces flow through it: the [otel] table's"
            " service_name and environment defaults do not apply to"
            " traces, and honouring an upstream sampling decision"
            " depends on the sampler that provider installed",
            wrapture.ConfigWarning,
            stacklevel=2,
        )
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
    application's own provider wins as the failsafe, with a warning
    naming what is lost. `export_interval` is
    seconds between exports; left as None, the reader falls back to
    OTEL_METRIC_EXPORT_INTERVAL and then its 60 second default.
    """

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )

    if isinstance(get_meter_provider(), MeterProvider):
        warnings.warn(
            "an OpenTelemetry meter provider is already configured, so"
            " wrapture's metrics flow through it: the [otel] table's"
            " service_name, export_interval and environment defaults"
            " do not apply to metrics",
            wrapture.ConfigWarning,
            stacklevel=2,
        )
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


def _resource(service_name: str | None) -> Any:
    from opentelemetry.sdk.resources import Resource

    return Resource.create({"service.name": service_name} if service_name else {})
