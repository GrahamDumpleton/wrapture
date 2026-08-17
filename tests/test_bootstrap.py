"""Tests for the autowrapt bootstrap entry point.

autowrapt itself is never installed here: the tests call bootstrap()
directly, the way autowrapt would at interpreter startup, and check
the entry point metadata that makes autowrapt find it. The double
opt-in (autowrapt installed, AUTOWRAPT_BOOTSTRAP=wrapture set) lives
entirely outside wrapture, so there is nothing else to test on this
side of the boundary.
"""

import textwrap
from importlib import metadata
from pathlib import Path

import pytest

from wrapture import ConfigWarning, Event, Sink
from wrapture.bootstrap import bootstrap


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


class Collector(Sink):
    def __init__(self) -> None:
        self.entered: list[Event] = []

    def on_enter(self, event: Event) -> None:
        self.entered.append(event)


# The config files below name this factory by module:attr reference;
# it hands back the collector the test inspects afterwards.

_last_collector: Collector | None = None


def make_collector() -> Collector:
    global _last_collector

    _last_collector = Collector()
    return _last_collector


def _config_text() -> str:
    return textwrap.dedent(
        f"""
        [[observe]]
        target = "{__name__}:Gateway"
        name = "charge"

        [sink]
        type = "{__name__}:make_collector"
        """
    )


def test_the_entry_point_is_declared_in_the_metadata() -> None:
    # AUTOWRAPT_BOOTSTRAP=wrapture names the entry point group, which
    # autowrapt hands to wrapt.discover_post_import_hooks(); the entry
    # name is the trigger module, and os is hooked because it is
    # always already imported when discovery runs, so the callback
    # fires immediately at startup.

    (entry,) = metadata.entry_points(group="wrapture", name="os")

    assert entry.value == "wrapture.bootstrap:bootstrap"
    assert entry.load() is bootstrap


def test_bootstrap_applies_the_discovered_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wrapture.toml").write_text(_config_text())
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WRAPTURE_CONFIG", raising=False)

    applied = bootstrap()
    assert applied is not None

    # The record also lands on the module attribute, the handle for
    # operator code reaching into an injected process.

    import wrapture.bootstrap

    assert wrapture.bootstrap.applied is applied

    try:
        Gateway().charge(500)
    finally:
        applied.revert()

    assert _last_collector is not None
    assert [event.path for event in _last_collector.entered] == [
        f"{__name__}:Gateway.charge"
    ]


def test_bootstrap_honours_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The config lives away from the working directory; WRAPTURE_CONFIG
    # names it, the same precedence chain the runner uses.

    source = tmp_path / "elsewhere" / "trace.toml"
    source.parent.mkdir()
    source.write_text(_config_text())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WRAPTURE_CONFIG", str(source))

    applied = bootstrap()
    assert applied is not None

    applied.revert()


def test_finding_no_config_warns_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The opt-in variable propagates to every python process in the
    # environment, so an interpreter without a config nearby must
    # start normally, with the gap made visible rather than fatal.

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WRAPTURE_CONFIG", raising=False)

    with pytest.warns(ConfigWarning, match="no config was found"):
        assert bootstrap() is None


def test_a_broken_config_warns_and_the_process_starts_untraced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Injection never takes the process down: an error raised here is
    # fatal to an interpreter that has not even started, and the
    # variable reaches every python in the environment. The loud
    # failure belongs to the runner and the programmatic path.

    (tmp_path / "wrapture.toml").write_text("not toml at all [")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WRAPTURE_CONFIG", raising=False)

    with pytest.warns(ConfigWarning, match="could not be applied"):
        assert bootstrap() is None
