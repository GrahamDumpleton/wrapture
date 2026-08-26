"""The logs signal: wrapture log events as OTel log records."""

from __future__ import annotations

import time
from typing import Any

from opentelemetry._logs import LogRecord, SeverityNumber, get_logger
from opentelemetry._logs import get_logger_provider as _get_logger_provider
from opentelemetry.context import Context
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    set_span_in_context,
)

import wrapture
from wrapture import Event

from ..sinks import _exception_level
from .common import _PREFIX, _exception_attributes
from .spans import OpenTelemetrySink


def _severity_number(levelno: int) -> SeverityNumber:
    # The standard mapping from Python logging levels onto the OTel
    # severity ranges: DEBUG=10 lands on 5, INFO=20 on 9, WARNING=30
    # on 13, ERROR=40 on 17, CRITICAL=50 on 21, with sub-level offsets
    # capped so a custom level between two standards stays in range.

    if levelno < 10:
        return SeverityNumber.TRACE

    clamped = min(levelno, 53)
    return SeverityNumber(4 * (clamped // 10) + 1 + min(clamped % 10, 3))


class OpenTelemetryLogsSink(wrapture.Sink):
    """Export kind "log" events through the OTel logs bridge.

    The mapping is direct: severity from the captured record's
    levelno, body from the formatted message, the logger name (the
    event's path) and the module, funcName and lineno data as
    attributes under the wrapture namespace, and the event's
    exception as the standard exception attributes. Selection stays
    with the [[log]] captures, which decide which messages become
    events at all; this sink only exports events that exist.

    Trace correlation is by reference and by parent link: a log event
    carries its tree's trace, giving the record its trace id, and the
    enclosing exported span is resolved from the span sink's
    open-span table via `parent_id`, so records land in backends
    attached to the right span, not just the right trace.

    Declaring "none" capture on both axes keeps the sink near-free:
    everything it exports is already on the event.
    """

    capture_args = "none"
    capture_result = "none"

    def __init__(
        self,
        *,
        logger_name: str = "wrapture",
        spans: OpenTelemetrySink | None = None,
        exceptions: str = "full",
    ) -> None:
        self._logger = get_logger(logger_name)
        self._spans = spans
        self._exceptions = _exception_level(exceptions)

        # The same clock pinning the span sink does: events are
        # stamped on perf_counter, OTel wants epoch nanoseconds.

        self._epoch_offset_ns = time.time_ns() - int(time.perf_counter() * 1e9)

        self.skipped = 0

    # -- wrapture.Sink protocol -----------------------------------------

    def on_enter(self, event: Event) -> None:
        """Emit the log event as one OTel log record.

        A log event is instantaneous, complete at its enter, so the
        record goes out here and the close notifications are ignored.
        """

        if event.kind != "log":
            self.skipped += 1
            return

        attributes: dict[str, Any] = {
            f"{_PREFIX}.logger": event.path,
            f"{_PREFIX}.module": str(event.data.get("module", "")),
            f"{_PREFIX}.funcName": str(event.data.get("funcName", "")),
            f"{_PREFIX}.lineno": int(event.data.get("lineno", 0)),
        }

        timestamp = None
        if event.started is not None:
            timestamp = self._epoch_offset_ns + int(event.started * 1e9)

        # At "full" the SDK derives the exception attributes, stacktrace
        # included; a reduced level supplies only what it allows.

        exception = event.exception
        if exception is not None and self._exceptions != "full":
            attributes.update(_exception_attributes(exception, self._exceptions))
            exception = None

        self._logger.emit(
            LogRecord(
                timestamp=timestamp,
                context=self._correlation(event),
                severity_text=str(event.data.get("level", "")),
                severity_number=_severity_number(int(event.data.get("levelno", 0))),
                body=event.data.get("message"),
                attributes=attributes,
                exception=exception,
            )
        )

    def flush(self) -> None:
        """Push batched records to the exporter; called by wrapture at
        interpreter exit and from flush_sinks()."""

        provider = _get_logger_provider()
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            force_flush()

    # -- internals -------------------------------------------------------

    def _correlation(self, event: Event) -> Context | None:
        # The record's trace identity as a Context: the trace id from
        # the tree's w3c slot, and the span id of the enclosing
        # exported span, resolved exactly through the span sink's
        # open-span table rather than the slot's register, which
        # another thread of the same tree may have moved. With no
        # enclosing exported span the span id is left invalid and the
        # record carries the trace id alone.

        if event.trace is None:
            return None

        slot = event.trace.slots.get("w3c")
        if slot is None:
            return None

        span_id = 0
        if self._spans is not None and event.parent_id is not None:
            with self._spans._lock:
                entry = self._spans._spans.get(event.parent_id)
            if entry is not None:
                span_id = entry[0].get_span_context().span_id

        flags = TraceFlags(TraceFlags.SAMPLED if slot.sampled else TraceFlags.DEFAULT)
        span_context = SpanContext(
            trace_id=int(slot.trace_id, 16),
            span_id=span_id,
            is_remote=False,
            trace_flags=flags,
        )

        return set_span_in_context(NonRecordingSpan(span_context))
