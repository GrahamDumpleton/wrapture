"""SDK pipeline setup: the span processor, sampler and resource the
span sink exports through, and the meter and logger providers the
other signals use, all built from config and the standard OTel
environment.

The posture is wrapture-first: choosing wrapture means taking all it
does, including standing up the export pipelines, which is what the
zero-code story requires, since in the config-driven case there is
no application code to configure the SDK. The pipelines are
wrapture's own, handed to its sinks directly; the SDK's global
providers are neither consulted nor installed, so an application
using the OTel API on its own account keeps whatever it set up, and
wrapture's telemetry is unaffected by it.
"""

from __future__ import annotations

import atexit
import logging
import os
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    DEFAULT_OFF,
    DEFAULT_ON,
    ParentBasedTraceIdRatio,
    Sampler,
    TraceIdRatioBased,
)

from .environment import _otlp_protocol

_logger = logging.getLogger(__name__)


def _trace_pipeline(
    service_name: str | None,
    sampler: str | None = None,
    sampler_arg: float | None = None,
) -> tuple[SpanProcessor, Resource, Sampler]:
    """Build the traces pipeline from the arguments and the standard
    OTel environment.

    OTEL_EXPORTER_OTLP_PROTOCOL picks the exporter (http/protobuf by
    default, or grpc), OTEL_EXPORTER_OTLP_ENDPOINT is read by the
    exporter itself, and OTEL_TRACES_EXPORTER=console swaps in the
    stdout exporter for a look without a collector. The sampler is
    `sampler` and `sampler_arg` when given, else OTEL_TRACES_SAMPLER
    and OTEL_TRACES_SAMPLER_ARG, else parentbased always-on, so an
    upstream "do not sample" decision carried in a remote parent is
    honoured.
    """

    return _span_processor(), _resource(service_name), _sampler(sampler, sampler_arg)


def _span_processor() -> SpanProcessor:
    # A batch processor around the exporter the environment names,
    # shut down at interpreter exit the way the SDK's tracer provider
    # would have arranged, so the last batch is exported.

    processor = BatchSpanProcessor(_span_exporter())
    atexit.register(processor.shutdown)

    return processor


def _span_exporter() -> SpanExporter:
    # The OTLP exporters read OTEL_EXPORTER_OTLP_ENDPOINT and its
    # friends themselves, so only the protocol choice happens here.

    if os.environ.get("OTEL_TRACES_EXPORTER") == "console":
        return ConsoleSpanExporter()

    if _otlp_protocol("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcSpanExporter,
        )

        grpc: SpanExporter = GrpcSpanExporter()
        return grpc

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )

    return OTLPSpanExporter()


def _sampler(name: str | None = None, arg: float | None = None) -> Sampler:
    """Resolve a sampler by its documented name.

    The names are the SDK's: always_on, always_off, traceidratio,
    parentbased_always_on (the default), parentbased_always_off and
    parentbased_traceidratio; the ratio forms take `arg` as the keep
    rate. Arguments not given fall back to OTEL_TRACES_SAMPLER and
    OTEL_TRACES_SAMPLER_ARG, resolved the way the SDK's tracer
    provider resolves them: an unknown name is warned about and falls
    back to the default, and a ratio that will not parse is taken as
    1.0.
    """

    if name is None:
        name = os.environ.get("OTEL_TRACES_SAMPLER", "parentbased_always_on")
    name = name.lower()

    fixed: dict[str, Sampler] = {
        "always_on": ALWAYS_ON,
        "always_off": ALWAYS_OFF,
        "parentbased_always_on": DEFAULT_ON,
        "parentbased_always_off": DEFAULT_OFF,
    }
    if name in fixed:
        return fixed[name]

    if name in ("traceidratio", "parentbased_traceidratio"):
        if arg is None:
            try:
                arg = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", ""))
            except ValueError:
                _logger.warning(
                    "OTEL_TRACES_SAMPLER_ARG is not a number; sampling at 1.0"
                )
                arg = 1.0

        if name == "traceidratio":
            return TraceIdRatioBased(arg)
        return ParentBasedTraceIdRatio(arg)

    _logger.warning("unknown sampler %r; using parentbased_always_on", name)

    return DEFAULT_ON


def _meter_provider(service_name: str | None, export_interval: float | None) -> Any:
    """Build a MeterProvider from the standard OTel environment.

    OTEL_EXPORTER_OTLP_PROTOCOL picks the exporter,
    OTEL_EXPORTER_OTLP_ENDPOINT is read by the exporter itself, and
    OTEL_METRICS_EXPORTER=console swaps in the stdout exporter.
    `export_interval` is seconds between exports; left as None, the
    reader falls back to OTEL_METRIC_EXPORT_INTERVAL and then its 60
    second default. The provider is wrapture's own, handed to the
    metrics sink rather than installed as the SDK's global.
    """

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )

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

    return MeterProvider(resource=_resource(service_name), metric_readers=[reader])


def _logger_provider(service_name: str | None) -> Any:
    """Build a LoggerProvider from the standard OTel environment.

    The logs half of what `_meter_provider` does for metrics:
    OTEL_EXPORTER_OTLP_PROTOCOL picks the exporter,
    OTEL_EXPORTER_OTLP_ENDPOINT is read by the exporter itself, and
    OTEL_LOGS_EXPORTER=console swaps in the stdout exporter. The
    provider is wrapture's own, handed to the logs sink.
    """

    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        BatchLogRecordProcessor,
        ConsoleLogRecordExporter,
    )

    exporter: Any
    if os.environ.get("OTEL_LOGS_EXPORTER") == "console":
        exporter = ConsoleLogRecordExporter()
    elif _otlp_protocol("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL") == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )

        exporter = OTLPLogExporter()
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )

        exporter = OTLPLogExporter()

    provider = LoggerProvider(resource=_resource(service_name))
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    return provider


def _resource(service_name: str | None) -> Resource:
    # Resource.create merges the standard OTEL_RESOURCE_ATTRIBUTES and
    # OTEL_SERVICE_NAME variables beneath what is given here.

    return Resource.create({"service.name": service_name} if service_name else {})
