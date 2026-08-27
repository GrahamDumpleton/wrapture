"""Tests for the OpenTelemetry export configuration.

These cover the top-level [otel] config table: presence opting in,
the enabled switch, registration ahead of the [[sink]] list, the
validation of the table's keys, both faces of the import guard
(the packages present building the sink, their absence failing the
load with the wrapture[otel] extra named), and the SDK posture
(wrapture standing up providers when none exist, an application's
provider winning as the warned failsafe).
"""

import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import wrapture
from wrapture import load_config

# On a plain install, without the wrapture[otel] extra, everything
# here would fail on the missing packages; the plain-install face of
# the import guard lives in test_otel_absent.py instead.

pytest.importorskip("opentelemetry")

# The module fixture below installs application providers, so every
# sink built through the config path defers to them and warns; the
# posture section asserts that warning deliberately, and the rest of
# the module ignores it as incidental.

pytestmark = pytest.mark.filterwarnings("ignore::wrapture.ConfigWarning")


@pytest.fixture(autouse=True, scope="module")
def _providers() -> Iterator[dict[str, Any]]:
    # Install SDK providers once for the module, so building the sink
    # under test never stands up real exporters with their network
    # endpoints and worker threads: the factory finds a provider
    # already configured and defers to it. Spans and log records land
    # synchronously in in-memory exporters, so the trace and log
    # tests can assert on what was actually exported.

    from opentelemetry import trace as otel_trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.metrics import set_meter_provider
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    otel_trace.set_tracer_provider(provider)

    logs = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
    set_logger_provider(logger_provider)

    set_meter_provider(MeterProvider())

    yield {"spans": spans, "logs": logs}


@pytest.fixture
def exported(_providers: dict[str, Any]) -> Any:
    # The module's in-memory span exporter, cleared for this test.

    _providers["spans"].clear()
    return _providers["spans"]


@pytest.fixture
def exported_logs(_providers: dict[str, Any]) -> Any:
    # The module's in-memory log exporter, cleared for this test.

    _providers["logs"].clear()
    return _providers["logs"]


# ---------------------------------------------------------------------------
# the [otel] table
# ---------------------------------------------------------------------------


def test_an_otel_table_builds_the_export_sink(tmp_path: Path) -> None:
    from wrapture.otel import OpenTelemetryMetricsSink, OpenTelemetrySink

    source = tmp_path / "wrapture.toml"
    source.write_text(
        "[otel]\n"
        'service_name = "shop"\n'
        'signals = ["traces", "metrics"]\n'
        "\n"
        "[otel.metrics]\n"
        "export_interval = 5\n"
    )

    config = load_config(source)

    assert isinstance(config.sink, wrapture.Fanout)
    spans, metrics = config.sink._sinks
    assert isinstance(spans, OpenTelemetrySink)
    assert isinstance(metrics, OpenTelemetryMetricsSink)


def test_a_lone_signal_builds_a_bare_sink(tmp_path: Path) -> None:
    from wrapture.otel import OpenTelemetrySink

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n')

    config = load_config(source)

    assert isinstance(config.sink, OpenTelemetrySink)


def test_the_otel_sink_registers_ahead_of_the_sink_list(tmp_path: Path) -> None:
    # Ordering by construction: whatever the [[sink]] list builds
    # stacks behind the [otel] sink, whichever comes first in the
    # file, so no other sink's on_enter can observe a root event
    # before the OTel sink has.

    from wrapture.otel import OpenTelemetrySink

    source = tmp_path / "wrapture.toml"
    source.write_text('[[sink]]\ntype = "printer"\n\n[otel]\nsignals = ["traces"]\n')

    config = load_config(source)

    assert isinstance(config.sink, wrapture.Fanout)
    first, second = config.sink._sinks
    assert isinstance(first, OpenTelemetrySink)
    assert isinstance(second, wrapture.Printer)


def test_the_otel_sink_prepends_to_a_fanned_out_sink_list(tmp_path: Path) -> None:
    # Several [[sink]] entries already fan out; the [otel] sink joins
    # that fan-out at the front rather than nesting a second layer.

    from wrapture.otel import OpenTelemetrySink

    source = tmp_path / "wrapture.toml"
    source.write_text(
        '[otel]\nsignals = ["traces"]\n\n'
        '[[sink]]\ntype = "printer"\n\n'
        '[[sink]]\ntype = "printer"\n'
    )

    config = load_config(source)

    assert isinstance(config.sink, wrapture.Fanout)
    sinks = config.sink._sinks
    assert len(sinks) == 3
    assert isinstance(sinks[0], OpenTelemetrySink)
    assert all(isinstance(each, wrapture.Printer) for each in sinks[1:])


def test_enabled_false_keeps_the_stanza_inert(tmp_path: Path) -> None:
    # A kept-but-off stanza, matching the [trace] style: with nothing
    # else registered there is no sink at all, and beside a [[sink]]
    # list the list stands alone.

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nenabled = false\nsignals = ["traces"]\n')

    assert load_config(source).sink is None

    beside = tmp_path / "beside.toml"
    beside.write_text('[otel]\nenabled = false\n\n[[sink]]\ntype = "printer"\n')

    assert isinstance(load_config(beside).sink, wrapture.Printer)


def test_a_bad_otel_table_fails_the_load(tmp_path: Path) -> None:
    not_a_table = tmp_path / "flat.toml"
    not_a_table.write_text("otel = true\n")

    with pytest.raises(wrapture.ConfigError, match="otel must be a table"):
        load_config(not_a_table)

    bad_key = tmp_path / "key.toml"
    bad_key.write_text('[otel]\nendpoint = "http://localhost:4318"\n')

    with pytest.raises(wrapture.ConfigError, match="unknown keys"):
        load_config(bad_key)

    bad_flag = tmp_path / "flag.toml"
    bad_flag.write_text('[otel]\nenabled = "yes"\n')

    with pytest.raises(wrapture.ConfigError, match="true or false"):
        load_config(bad_flag)

    bad_signal = tmp_path / "signal.toml"
    bad_signal.write_text('[otel]\nsignals = ["profiles"]\n')

    with pytest.raises(wrapture.ConfigError, match="building the export sink"):
        load_config(bad_signal)


# ---------------------------------------------------------------------------
# the import guard
# ---------------------------------------------------------------------------


def test_a_missing_extra_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate the packages being absent by evicting the subpackage
    # from the module cache (and the parent package's attribute, which
    # `from . import otel` would otherwise return unrefreshed) and
    # blocking opentelemetry itself, so the deferred import fails the
    # way a plain install's would.

    for name in [
        each
        for each in sys.modules
        if each == "wrapture.otel" or each.startswith("wrapture.otel.")
    ]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.delattr(wrapture, "otel", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n')

    with pytest.raises(wrapture.ConfigError, match=r"wrapture\[otel\]"):
        load_config(source)


def test_a_broken_factory_call_reports_the_cause(tmp_path: Path) -> None:
    source = tmp_path / "wrapture.toml"
    source.write_text("[otel]\n\n[otel.metrics]\nexport_interval = -1\n")

    with pytest.raises(wrapture.ConfigError, match="positive number of seconds"):
        load_config(source)


# ---------------------------------------------------------------------------
# the SDK posture: wrapture-first, app provider as the failsafe
# ---------------------------------------------------------------------------


def test_an_existing_tracer_provider_wins_with_a_warning(tmp_path: Path) -> None:
    # The module fixture installed an application provider, so the
    # factory defers to it and the warning names what is lost.

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n')

    with pytest.warns(wrapture.ConfigWarning, match="tracer provider is already"):
        load_config(source)


def test_an_existing_meter_provider_wins_with_a_warning(tmp_path: Path) -> None:
    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["metrics"]\n')

    with pytest.warns(wrapture.ConfigWarning, match="meter provider is already"):
        load_config(source)


def test_no_tracer_provider_stands_one_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # The wrapture-first path: nothing configured (the API's default
    # proxy provider is not an SDK one), so the factory stands up a
    # provider from its arguments and the environment, silently. The
    # global setters are patched to observe rather than install.

    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider

    from wrapture.otel.providers import _configure_provider

    installed: list[Any] = []
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: object())
    monkeypatch.setattr(otel_trace, "set_tracer_provider", installed.append)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")

    with warnings.catch_warnings():
        warnings.simplefilter("error", wrapture.ConfigWarning)
        _configure_provider("shop")

    (provider,) = installed
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "shop"


def test_no_meter_provider_stands_one_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.metrics import MeterProvider

    from wrapture.otel import providers

    installed: list[Any] = []
    monkeypatch.setattr(providers, "get_meter_provider", lambda: object())
    monkeypatch.setattr(providers, "set_meter_provider", installed.append)
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "console")

    with warnings.catch_warnings():
        warnings.simplefilter("error", wrapture.ConfigWarning)
        providers._configure_meter_provider("shop", export_interval=5)

    (provider,) = installed
    assert isinstance(provider, MeterProvider)
    assert provider._sdk_config.resource.attributes["service.name"] == "shop"


# ---------------------------------------------------------------------------
# trace completion: claiming, parenting, sampling
# ---------------------------------------------------------------------------

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def _apply_traces(tmp_path: Path) -> Any:
    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n')
    return load_config(source).apply()


def _parent_id(headers: dict[str, str]) -> str:
    return headers["traceparent"].split("-")[2]


def _serve(app: Any, environ: dict[str, Any]) -> None:
    body = app(environ, lambda status, headers: None)
    list(body)
    body.close()


def test_a_minted_identity_is_replaced_with_the_sdk_id(
    tmp_path: Path, exported: Any
) -> None:
    # Claim-time identity replacement: the root span is created
    # normally, the SDK generating its own trace id, and the slot
    # takes the whole identity, so the serialised record, outbound
    # headers and the exported span all read one id and the backend
    # shows a clean native root.

    from wrapture.sinks import _event_record

    applied = _apply_traces(tmp_path)
    try:
        with wrapture.timeline() as tape:
            with wrapture.block("outer"):
                headers = wrapture.trace_headers()
    finally:
        applied.revert()

    (span,) = exported.get_finished_spans()
    event = tape.all[0]
    assert event.trace is not None
    slot = event.trace.slots["w3c"]

    assert slot.claimed
    assert slot.trace_id == format(span.context.trace_id, "032x")
    assert headers["traceparent"].split("-")[1] == slot.trace_id
    assert _parent_id(headers) == format(span.context.span_id, "016x")
    assert span.parent is None

    record = _event_record(event)
    assert record["trace"]["w3c"]["trace_id"] == slot.trace_id


def test_an_arrived_identity_continues_the_callers_trace(
    tmp_path: Path, exported: Any
) -> None:
    # Remote root parenting: an identity that arrived in headers is
    # never replaced; the root span continues the caller's trace with
    # a remote parent, and inside the request outbound headers parent
    # downstream services onto the exported request span.

    captured: dict[str, Any] = {}

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        captured["headers"] = wrapture.trace_headers()
        start_response("200 OK", [])
        return [b"ok"]

    applied = _apply_traces(tmp_path)
    try:
        _serve(
            wrapture.WSGIMiddleware(app),
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/x",
                "HTTP_TRACEPARENT": TRACEPARENT,
            },
        )
    finally:
        applied.revert()

    (span,) = exported.get_finished_spans()

    assert format(span.context.trace_id, "032x") == ("0af7651916cd43dd8448eb211c80319c")
    assert span.parent is not None and span.parent.is_remote
    assert format(span.parent.span_id, "016x") == "b7ad6b7169203331"

    assert captured["headers"]["traceparent"].split("-")[1] == (
        "0af7651916cd43dd8448eb211c80319c"
    )
    assert _parent_id(captured["headers"]) == format(span.context.span_id, "016x")


def test_the_register_tracks_the_innermost_open_span(
    tmp_path: Path, exported: Any
) -> None:
    # The register takes each span's id as it opens and is restored
    # to the enclosing span's as it closes, so trace_headers() always
    # names a live exported parent.

    seen: list[dict[str, str]] = []

    applied = _apply_traces(tmp_path)
    try:
        with wrapture.block("outer"):
            seen.append(wrapture.trace_headers())
            with wrapture.block("inner"):
                seen.append(wrapture.trace_headers())
            seen.append(wrapture.trace_headers())
    finally:
        applied.revert()

    inner_span, outer_span = exported.get_finished_spans()

    assert _parent_id(seen[0]) == format(outer_span.context.span_id, "016x")
    assert _parent_id(seen[1]) == format(inner_span.context.span_id, "016x")
    assert _parent_id(seen[2]) == _parent_id(seen[0])

    assert len({headers["traceparent"].split("-")[1] for headers in seen}) == 1


def test_an_unsampled_upstream_decision_is_honoured(
    tmp_path: Path, exported: Any
) -> None:
    # The parent-based sampler sees the arrived sampled flag on the
    # remote parent: flags 00 means the upstream said do not sample,
    # and no span is exported for the tree.

    unsampled = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b"ok"]

    applied = _apply_traces(tmp_path)
    try:
        _serve(
            wrapture.WSGIMiddleware(app),
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/x",
                "HTTP_TRACEPARENT": unsampled,
            },
        )
    finally:
        applied.revert()

    assert exported.get_finished_spans() == ()


def test_a_wrapture_sampled_out_tree_keeps_the_minted_id(
    tmp_path: Path, exported: Any
) -> None:
    # wrapture's own sample gate drops the tree before the sink hears
    # it, so the minted identity stands unclaimed and outbound
    # headers carry it, which is what "not sampled" means.

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n\n[otel.traces]\nsample = 0.0\n')

    applied = load_config(source).apply()
    try:
        with wrapture.timeline() as tape:
            with wrapture.block("outer"):
                headers = wrapture.trace_headers()
    finally:
        applied.revert()

    assert exported.get_finished_spans() == ()

    event = tape.all[0]
    assert event.trace is not None
    slot = event.trace.slots["w3c"]
    assert not slot.claimed
    assert headers["traceparent"].split("-")[1] == slot.trace_id


# ---------------------------------------------------------------------------
# the logs signal
# ---------------------------------------------------------------------------


def test_a_log_inside_an_exported_span_correlates_to_it(
    tmp_path: Path, exported: Any, exported_logs: Any
) -> None:
    # The payoff of the kind-gate trace inheritance: the record lands
    # with the tree's trace id and the enclosing exported span's id,
    # severity and body mapped from the captured logging record.

    import logging

    source = tmp_path / "wrapture.toml"
    source.write_text(
        '[otel]\nsignals = ["traces", "logs"]\n\n[[log]]\nname = "otel_demo"\n'
    )

    applied = load_config(source).apply()
    try:
        with wrapture.block("outer"):
            logging.getLogger("otel_demo").warning("boom %s", "x")
    finally:
        applied.revert()

    (span,) = exported.get_finished_spans()
    (data,) = exported_logs.get_finished_logs()
    record = data.log_record

    assert record.trace_id == span.context.trace_id
    assert record.span_id == span.context.span_id
    assert record.body == "boom x"
    assert record.severity_text == "WARNING"
    assert record.severity_number is not None and record.severity_number.value == 13
    assert record.attributes is not None
    assert record.attributes["wrapture.logger"] == "otel_demo"
    assert record.attributes["wrapture.lineno"] > 0


def test_a_log_without_traces_carries_the_minted_trace_id(
    tmp_path: Path, exported_logs: Any
) -> None:
    # With only the logs signal on, the tree still mints an identity,
    # so the record carries the trace id with no span to point at.

    import logging

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["logs"]\n\n[[log]]\nname = "otel_lone"\n')

    applied = load_config(source).apply()
    try:
        with wrapture.timeline() as tape:
            with wrapture.block("outer"):
                logging.getLogger("otel_lone").error("alone")
    finally:
        applied.revert()

    (data,) = exported_logs.get_finished_logs()
    record = data.log_record

    block = tape.all[0]
    assert block.trace is not None
    assert record.trace_id == int(block.trace.slots["w3c"].trace_id, 16)
    assert not record.span_id
    assert record.severity_number is not None and record.severity_number.value == 17


def test_a_root_log_has_no_trace_correlation(
    tmp_path: Path, exported_logs: Any
) -> None:
    # A log outside any operation carries no trace, per the kind
    # gate, and the record says so.

    import logging

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["logs"]\n\n[[log]]\nname = "otel_root"\n')

    applied = load_config(source).apply()
    try:
        logging.getLogger("otel_root").warning("floating")
    finally:
        applied.revert()

    (data,) = exported_logs.get_finished_logs()
    record = data.log_record

    assert not record.trace_id
    assert not record.span_id


def test_a_logged_exception_maps_to_exception_attributes(
    tmp_path: Path, exported_logs: Any
) -> None:
    import logging

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["logs"]\n\n[[log]]\nname = "otel_exc"\n')

    applied = load_config(source).apply()
    try:
        try:
            raise KeyError("missing")
        except KeyError:
            logging.getLogger("otel_exc").exception("lookup failed")
    finally:
        applied.revert()

    (data,) = exported_logs.get_finished_logs()
    record = data.log_record

    assert record.attributes is not None
    assert record.attributes["exception.type"] == "KeyError"
    assert "missing" in str(record.attributes["exception.message"])


@wrapture.observed
def _chore() -> str:
    return "done"


def test_span_times_come_from_the_sinks_pinned_clock(
    tmp_path: Path, exported: Any
) -> None:
    # The recording path delivers on_enter before stamping
    # event.started, so the sink must take the start from its own
    # pinned clock rather than letting the SDK stamp wall-clock now:
    # start and end then live on one timeline, and a short span can
    # never come out negative however far the wall clock drifts from
    # perf_counter after the pinning. Shifting the pinned offset must
    # move both ends of the span together.

    import time

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n')

    config = load_config(source)
    sink: Any = config.sink

    hour = 3_600_000_000_000_000
    sink._epoch_offset_ns += hour

    applied = config.apply()
    try:
        _chore()
    finally:
        applied.revert()

    (span,) = exported.get_finished_spans()

    assert span.start_time is not None and span.end_time is not None
    assert span.start_time > time.time_ns() + hour // 2
    assert span.end_time >= span.start_time


# ---------------------------------------------------------------------------
# the fork story
# ---------------------------------------------------------------------------


def test_on_fork_drops_the_open_span_table(tmp_path: Path, exported: Any) -> None:
    # In a child the in-flight spans belong to the parent, which will
    # close them: on_fork drops the table, and the parent-side close
    # arriving in the child (as it would never actually do) or the
    # child's own close of a dropped span is simply not found, with
    # nothing exported twice.

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n')

    config = load_config(source)
    sink: Any = config.sink

    applied = config.apply()
    try:
        with wrapture.block("outer"):
            assert sink.open_spans == 1

            sink.on_fork()

            assert sink.open_spans == 0
    finally:
        applied.revert()

    # The close after on_fork found no span, so nothing was exported
    # for the dropped one.

    assert exported.get_finished_spans() == ()


# ---------------------------------------------------------------------------
# events reach the configured sink
# ---------------------------------------------------------------------------


def test_a_configured_otel_sink_hears_events(tmp_path: Path) -> None:
    # End to end through the config path: an [otel] registration with
    # the traces signal opens and closes spans for a recorded tree.

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n')

    config = load_config(source)
    sink: Any = config.sink

    applied = config.apply()
    try:
        with wrapture.block("outer"):
            with wrapture.block("inner"):
                pass
    finally:
        applied.revert()

    assert sink.open_spans == 0
    assert sink.skipped == 0


# ---------------------------------------------------------------------------
# noted exceptions
# ---------------------------------------------------------------------------


class _Pricing:
    def quote(self, sku: str) -> int:
        if sku == "missing":
            raise KeyError(sku)
        return 100


class _Shop:
    """The framework shape: dispatch() catches what the view raises
    and hands it to handle_error(), which returns normally."""

    def dispatch(self, sku: str) -> str:
        try:
            return f"200 {_Pricing().quote(sku)}"
        except Exception as exc:
            return self.handle_error(exc)

    def handle_error(self, exc: BaseException) -> str:
        return "500"


def _noting_handler(dispatch: Any) -> Any:
    def noting(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        wrapture.current_event(binding=dispatch).note_exception(args[0])
        return wrapped(*args, **kwargs)

    return wrapture.binding(_Shop, "handle_error").on_call.decorates(noting)


def _span_named(exported: Any, name: str) -> Any:
    return next(span for span in exported.get_finished_spans() if span.name == name)


def test_a_noted_exception_becomes_a_span_event_and_an_error_status(
    tmp_path: Path, exported: Any
) -> None:
    from opentelemetry.trace import StatusCode

    dispatch = wrapture.binding(_Shop, "dispatch")
    handle = _noting_handler(dispatch)

    applied = _apply_traces(tmp_path)
    try:
        with dispatch, handle:
            assert _Shop().dispatch("missing") == "500"
    finally:
        applied.revert()

    span = _span_named(exported, "test_otel:_Shop.dispatch")

    # The span closed normally, with its result, and still reports
    # the failure: one exception event, timestamped inside the span,
    # and an error status naming the type.

    assert span.attributes["wrapture.result"] == "500"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "KeyError"

    (occurrence,) = span.events
    assert occurrence.name == "exception"
    assert occurrence.attributes["exception.type"] == "KeyError"
    assert span.start_time <= occurrence.timestamp <= span.end_time

    # The handler's own span, which the note was aimed past, is clean.

    handler = _span_named(exported, "test_otel:_Shop.handle_error")
    assert handler.status.status_code is StatusCode.UNSET
    assert handler.events == ()


def test_a_noted_exception_and_a_5xx_agree_on_one_error_status(
    tmp_path: Path, exported: Any
) -> None:
    from opentelemetry.trace import StatusCode

    class App:
        def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
            try:
                _Pricing().quote("missing")
            except KeyError as exc:
                status = self.handle_exception(exc)
            else:
                status = "200 OK"
            start_response(status, [])
            return [b""]

        def handle_exception(self, exc: BaseException) -> str:
            return "500 INTERNAL SERVER ERROR"

    def noting(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        wrapture.current_event(kind="request").note_exception(args[0])
        return wrapped(*args, **kwargs)

    handle = wrapture.binding(App, "handle_exception").on_call.decorates(noting)
    app = wrapture.WSGIMiddleware(App())

    applied = _apply_traces(tmp_path)
    try:
        with handle:
            _serve(app, {"REQUEST_METHOD": "GET", "PATH_INFO": "/quote/missing"})
    finally:
        applied.revert()

    request = next(
        span for span in exported.get_finished_spans() if span.kind.name == "SERVER"
    )

    assert request.attributes["http.response.status_code"] == 500
    assert request.status.status_code is StatusCode.ERROR
    assert [event.attributes["exception.type"] for event in request.events] == [
        "KeyError"
    ]


def test_a_noted_exception_that_escapes_is_one_span_event(
    tmp_path: Path, exported: Any
) -> None:
    from opentelemetry.trace import StatusCode

    error = KeyError("missing")

    def note_then_raise(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        wrapture.note_exception(error)
        raise error

    dispatch = wrapture.binding(_Shop, "dispatch").on_call.decorates(note_then_raise)

    applied = _apply_traces(tmp_path)
    try:
        with dispatch, pytest.raises(KeyError):
            _Shop().dispatch("missing")
    finally:
        applied.revert()

    span = _span_named(exported, "test_otel:_Shop.dispatch")

    assert span.status.status_code is StatusCode.ERROR
    assert [event.attributes["exception.type"] for event in span.events] == ["KeyError"]


def test_metrics_attribute_a_noted_exception_as_the_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from wrapture.otel import metrics as metrics_module

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics_module, "get_meter", provider.get_meter)

    sink = metrics_module.OpenTelemetryMetricsSink()
    dispatch = wrapture.binding(_Shop, "dispatch")
    handle = _noting_handler(dispatch)

    wrapture.add_sink(sink)
    try:
        with dispatch, handle:
            _Shop().dispatch("missing")
            _Shop().dispatch("sku")
    finally:
        wrapture.remove_sink(sink)

    data: Any = reader.get_metrics_data()
    points: list[tuple[Any, int]] = [
        (point.attributes, point.count)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "wrapture.call.duration"
        for point in metric.data.data_points
    ]

    dispatch_path = f"{__name__}:_Shop.dispatch"
    by_error = {
        attributes.get("error.type"): count
        for attributes, count in points
        if attributes["wrapture.path"] == dispatch_path
    }

    # The failed dispatch is attributed by the noted type, the clean
    # one carries no error attribute, and the handler's own call,
    # which the note was aimed past, is not an error either.

    assert by_error == {"KeyError": 1, None: 1}
    assert all(
        "error.type" not in attributes
        for attributes, _ in points
        if attributes["wrapture.path"] == f"{__name__}:_Shop.handle_error"
    )


# ---------------------------------------------------------------------------
# the request data-key contract: route
# ---------------------------------------------------------------------------


class _Router:
    """The framework shape: routing matches inside the request, after
    the request event has opened, and the app annotates the match."""

    def __init__(self, route: str | None) -> None:
        self.route = route

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        self.dispatch()
        start_response("200 OK", [])
        return [b""]

    def dispatch(self) -> None:
        if self.route is not None:
            wrapture.annotate(route=self.route, endpoint="quote")


def _request_span(exported: Any) -> Any:
    return next(
        span for span in exported.get_finished_spans() if span.kind.name == "SERVER"
    )


def test_a_route_annotation_renames_the_span_and_sets_http_route(
    tmp_path: Path, exported: Any
) -> None:
    app = wrapture.WSGIMiddleware(_Router("/quote/<item>"))

    applied = _apply_traces(tmp_path)
    try:
        _serve(app, {"REQUEST_METHOD": "GET", "PATH_INFO": "/quote/widget"})
    finally:
        applied.revert()

    span = _request_span(exported)

    # The span is named by the low-cardinality pattern, the raw path
    # stays under url.path, and the route rides under its semconv name
    # rather than being repeated as wrapture.data.route. The endpoint
    # has no semconv name and stays ordinary data.

    assert span.name == "GET /quote/<item>"
    assert span.attributes["http.route"] == "/quote/<item>"
    assert span.attributes["url.path"] == "/quote/widget"
    assert "wrapture.data.route" not in span.attributes
    assert span.attributes["wrapture.data.endpoint"] == "quote"


def test_a_request_matching_no_route_keeps_its_path_name(
    tmp_path: Path, exported: Any
) -> None:
    app = wrapture.WSGIMiddleware(_Router(None))

    applied = _apply_traces(tmp_path)
    try:
        _serve(app, {"REQUEST_METHOD": "GET", "PATH_INFO": "/nowhere"})
    finally:
        applied.revert()

    span = _request_span(exported)

    assert span.name == "GET /nowhere"
    assert "http.route" not in span.attributes


def test_metrics_attribute_requests_by_route(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from wrapture.otel import metrics as metrics_module

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics_module, "get_meter", provider.get_meter)

    sink = metrics_module.OpenTelemetryMetricsSink()
    routed = wrapture.WSGIMiddleware(_Router("/quote/<item>"))
    unrouted = wrapture.WSGIMiddleware(_Router(None))

    wrapture.add_sink(sink)
    try:
        _serve(routed, {"REQUEST_METHOD": "GET", "PATH_INFO": "/quote/widget"})
        _serve(routed, {"REQUEST_METHOD": "GET", "PATH_INFO": "/quote/gadget"})
        _serve(unrouted, {"REQUEST_METHOD": "GET", "PATH_INFO": "/nowhere"})
    finally:
        wrapture.remove_sink(sink)

    data: Any = reader.get_metrics_data()
    by_route = {
        point.attributes.get("http.route"): point.count
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "http.server.request.duration"
        for point in metric.data.data_points
    }

    # Two URLs under one pattern aggregate into one series; the
    # unrouted request has no route attribute at all, never a blank.

    assert by_route == {"/quote/<item>": 2, None: 1}


# ---------------------------------------------------------------------------
# the exceptions= level
# ---------------------------------------------------------------------------


def _apply_otel(tmp_path: Path, body: str) -> Any:
    source = tmp_path / "wrapture.toml"
    source.write_text(body)
    return load_config(source).apply()


def _exception_event(span: Any) -> Any:
    return next(event for event in span.events if event.name == "exception")


def test_a_full_exception_carries_message_and_stacktrace(
    tmp_path: Path, exported: Any
) -> None:
    dispatch = wrapture.binding(_Shop, "dispatch")

    applied = _apply_otel(tmp_path, '[otel]\nsignals = ["traces"]\n')
    try:
        with dispatch, _noting_handler(dispatch):
            _Shop().dispatch("missing")
    finally:
        applied.revert()

    event = _exception_event(_span_named(exported, "test_otel:_Shop.dispatch"))

    assert event.attributes["exception.type"] == "KeyError"
    assert "missing" in event.attributes["exception.message"]
    assert "Traceback" in event.attributes["exception.stacktrace"]


def test_message_level_drops_the_stacktrace(tmp_path: Path, exported: Any) -> None:
    dispatch = wrapture.binding(_Shop, "dispatch")

    applied = _apply_otel(
        tmp_path, '[otel]\nsignals = ["traces"]\nexceptions = "message"\n'
    )
    try:
        with dispatch, _noting_handler(dispatch):
            _Shop().dispatch("missing")
    finally:
        applied.revert()

    event = _exception_event(_span_named(exported, "test_otel:_Shop.dispatch"))

    assert event.attributes["exception.type"] == "KeyError"
    assert "missing" in event.attributes["exception.message"]
    assert "exception.stacktrace" not in event.attributes


def test_type_level_keeps_only_the_type_on_spans_and_logs(
    tmp_path: Path, exported: Any, exported_logs: Any
) -> None:
    import logging

    from opentelemetry.trace import StatusCode

    applied = _apply_otel(
        tmp_path,
        '[otel]\nsignals = ["traces", "logs"]\nexceptions = "type"\n\n'
        '[[log]]\nname = "otel_level"\n',
    )
    try:
        with wrapture.binding(_Pricing, "quote"), pytest.raises(KeyError):
            _Pricing().quote("missing")
        try:
            raise KeyError("secret")
        except KeyError:
            logging.getLogger("otel_level").exception("lookup failed")
    finally:
        applied.revert()

    span = _span_named(exported, "test_otel:_Pricing.quote")
    event = _exception_event(span)

    # The escaped exception is still an error with its type, but the
    # secret in the message goes nowhere: not the span event, not the
    # status description, not the log record.

    assert span.status.status_code is StatusCode.ERROR
    assert dict(event.attributes) == {"exception.type": "KeyError"}

    (data,) = exported_logs.get_finished_logs()
    attributes = data.log_record.attributes
    assert attributes["exception.type"] == "KeyError"
    assert "exception.message" not in attributes
    assert "exception.stacktrace" not in attributes


def test_an_unknown_exceptions_level_fails_the_load(tmp_path: Path) -> None:
    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nexceptions = "short"\n')

    with pytest.raises(wrapture.ConfigError, match="exceptions must be one of"):
        load_config(source)


# ---------------------------------------------------------------------------
# event links become span links
# ---------------------------------------------------------------------------


def test_a_detached_root_links_to_the_origins_exported_span(
    tmp_path: Path, exported: Any
) -> None:
    # The link captured at the hand-off carries the register's ids,
    # which the sink keeps at the innermost exported span, so the
    # detached root's span links to the origin's real span and
    # neither span parents the other.

    import threading

    def work() -> None:
        with wrapture.block("work"):
            pass

    # The detached work runs after the origin has closed, the
    # fire-and-forget shape, so ambient parenting could never have
    # related the two.

    applied = _apply_traces(tmp_path)
    try:
        with wrapture.block("request"):
            thread = threading.Thread(target=wrapture.detach(work))
        thread.start()
        thread.join()
    finally:
        applied.revert()

    spans = {span.name: span for span in exported.get_finished_spans()}
    request, work_span = spans["request"], spans["work"]

    assert work_span.parent is None
    assert work_span.context.trace_id != request.context.trace_id

    (link,) = work_span.links
    assert link.context.trace_id == request.context.trace_id
    assert link.context.span_id == request.context.span_id
    assert not link.context.is_remote
    assert not link.attributes


def test_a_consumer_blocks_header_link_is_remote(tmp_path: Path, exported: Any) -> None:
    applied = _apply_traces(tmp_path)
    try:
        with wrapture.block("consume", links=[{"traceparent": TRACEPARENT}]):
            pass
    finally:
        applied.revert()

    (span,) = exported.get_finished_spans()
    (link,) = span.links

    assert span.parent is None
    assert link.context.is_remote
    assert format(link.context.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"
    assert format(link.context.span_id, "016x") == "b7ad6b7169203331"


def test_link_attributes_ride_along_and_idless_links_are_left_out(
    tmp_path: Path, exported: Any
) -> None:
    from wrapture import EventLink

    applied = _apply_traces(tmp_path)
    try:
        with wrapture.block(
            "consume",
            links=[
                EventLink(trace_id="a" * 32, span_id="b" * 16, attributes={"n": 7}),
                EventLink(seq=12),
            ],
        ):
            pass
    finally:
        applied.revert()

    (span,) = exported.get_finished_spans()
    (link,) = span.links

    assert format(link.context.span_id, "016x") == "b" * 16
    assert dict(link.attributes) == {"n": 7}
