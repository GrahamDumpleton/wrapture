"""The plain-install face of the [otel] import guard.

These run only where the OpenTelemetry packages are genuinely
absent, which is what the no-extra CI leg installs, and are skipped
in the ordinary dev environment where the packages are present (the
simulated-absence face lives in test_otel.py). Together with the
rest of the suite passing on a plain install, they keep the promise
that base wrapture neither needs nor touches OpenTelemetry.
"""

import importlib.util
from pathlib import Path

import pytest

import wrapture

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("opentelemetry") is not None,
    reason="the OpenTelemetry packages are installed",
)


def test_the_otel_table_names_the_missing_extra(tmp_path: Path) -> None:
    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nsignals = ["traces"]\n')

    with pytest.raises(wrapture.ConfigError, match=r"wrapture\[otel\]"):
        wrapture.load_config(source)


def test_an_inert_otel_table_needs_no_packages(tmp_path: Path) -> None:
    # enabled = false never reaches the import, so a config carrying
    # a switched-off stanza loads fine on a plain install.

    source = tmp_path / "wrapture.toml"
    source.write_text('[otel]\nenabled = false\n\n[[sink]]\ntype = "printer"\n')

    assert isinstance(wrapture.load_config(source).sink, wrapture.Printer)
