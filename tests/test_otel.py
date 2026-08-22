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

# The module fixture below installs application providers, so every
# sink built through the config path defers to them and warns; the
# posture section asserts that warning deliberately, and the rest of
# the module ignores it as incidental.

pytestmark = pytest.mark.filterwarnings("ignore::wrapture.ConfigWarning")


@pytest.fixture(autouse=True, scope="module")
def _providers() -> Iterator[None]:
    # Install SDK providers once for the module, so building the sink
    # under test never stands up real exporters with their network
    # endpoints and worker threads: the factory finds a provider
    # already configured and defers to it.

    from opentelemetry import trace as otel_trace
    from opentelemetry.metrics import set_meter_provider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    otel_trace.set_tracer_provider(TracerProvider())
    set_meter_provider(MeterProvider())

    yield


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
