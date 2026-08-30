"""The metrics signal: wrapture events aggregated into OTel metrics."""

from __future__ import annotations

from opentelemetry.metrics import MeterProvider, get_meter, get_meter_provider
from opentelemetry.util.types import AttributeValue

import wrapture
from wrapture import Event

from .common import _PREFIX, _status_code


class OpenTelemetryMetricsSink(wrapture.Sink):
    """Aggregate wrapture events into OTel metrics.

    Where the span sink exports each event individually, this one only
    counts and times: request durations into the semantic-convention
    HTTP histogram attributed by method and status, call durations
    into a per-path histogram, and a counter of operations observed
    beginning. The set of bound paths is closed, chosen by the config,
    which is what makes the path a safe metric attribute; the raw
    request URL is unbounded, so requests are attributed by method,
    status and, when the request was annotated with one, the matched
    route pattern, which is closed the same way.

    Declaring "none" capture on both axes keeps the sink near-free: a
    metrics-only deployment records timings and outcomes without ever
    capturing a value.
    """

    capture_args = "none"
    capture_result = "none"

    def __init__(
        self, *, provider: MeterProvider | None = None, meter_name: str = "wrapture"
    ) -> None:
        # The provider is wrapture's own when the factory built it;
        # left unspecified, the SDK's global one is used.

        self._provider = provider if provider is not None else get_meter_provider()
        meter = (
            provider.get_meter(meter_name)
            if provider is not None
            else get_meter(meter_name)
        )

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
        """Record the completed operation's duration; one carrying a
        noted exception is attributed by the first one's type, so the
        error rate counts failures the code handled itself."""

        caught = event.caught
        error = type(caught[0].exception).__name__ if caught else None
        self._record(event, error=error)

    def on_error(self, event: Event) -> None:
        """Record the failed operation's duration, attributed by the
        exception type."""

        exception = event.exception
        error = type(exception).__name__ if exception is not None else "error"
        self._record(event, error=error)

    def flush(self) -> None:
        """Push the current aggregation out through the exporter."""

        force_flush = getattr(self._provider, "force_flush", None)
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
            route = event.data.get("route")
            if route:
                attributes["http.route"] = str(route)
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
