"""Tests for the instrumentation command of wrapture.tools: the
listing, the --config marking, the --toml template and its --enabled
and --config variants, and the usage errors.

As in test_instrumentation.py, registered instrumentation comes from
real dist-info directories written under tmp_path, so the command
sees exactly what importlib.metadata sees.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import textwrap
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from wrapture.tools.__main__ import main as tools_main
from wrapture.tools.instrumentation import _parse
from wrapture.tools.instrumentation import main as command_main

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules() -> Iterator[None]:
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("cfgt"):
            del sys.modules[name]


def _install(
    site: Path,
    *,
    distribution: str,
    version: str,
    entries: Mapping[str, str],
    summary: str = "",
    top_level: tuple[str, ...] = (),
    url: str | None = None,
) -> None:
    info = site / f"{distribution.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)

    lines = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {version}",
    ]
    if summary:
        lines.append(f"Summary: {summary}")
    if url:
        lines.append(f"Home-page: {url}")
    (info / "METADATA").write_text("\n".join(lines) + "\n")

    points = "".join(f"{name} = {value}\n" for name, value in entries.items())
    (info / "entry_points.txt").write_text(f"[wrapture.instrumentation]\n{points}")

    if top_level:
        (info / "top_level.txt").write_text("\n".join(top_level) + "\n")


FLASKISH = '''
import wrapture

class FlaskishInstrumentation(wrapture.Instrumentation):
    """Request, view and blueprint tracing for Flaskish applications."""

    target = "cfgt_flaskish"
    supports = ">=2.0,<4"
    requires = "cfgt_werkzeugish"
    removable = True
    settings = {
        "capture_headers": wrapture.Setting(False, "record request headers"),
        "ignore_paths": wrapture.Setting((), "paths never traced, exact match"),
        "timeout": wrapture.Setting(None, "seconds before a slow view is flagged"),
    }

    @wrapture.instrumentation_hook("cfgt_flaskish.app")
    def app(self, name, module):
        pass

    @wrapture.instrumentation_hook("cfgt_flaskish.sansio", supports=">=3.5")
    def sansio(self, name, module):
        pass
'''

WERKZEUGISH = """
import wrapture

class WerkzeugishInstrumentation(wrapture.Instrumentation):
    target = "cfgt_werkzeugish"

    @wrapture.instrumentation_hook("cfgt_werkzeugish.routing")
    def routing(self, name, module):
        pass
"""


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Two distributions: one for flaskish with its target installed at
    # 3.1.0, one for werkzeugish with no target installed.

    site = tmp_path / "site"
    site.mkdir()
    (site / "cfgt_pkg_flaskish.py").write_text(FLASKISH)
    (site / "cfgt_pkg_werkzeugish.py").write_text(WERKZEUGISH)

    _install(
        site,
        distribution="wrapture-instrumentation-flaskish",
        version="1.2.0",
        entries={"flaskish": "cfgt_pkg_flaskish:FlaskishInstrumentation"},
        url="https://example.test/flaskish",
    )
    _install(
        site,
        distribution="wrapture-instrumentation-werkzeugish",
        version="0.3",
        entries={"werkzeugish": "cfgt_pkg_werkzeugish:WerkzeugishInstrumentation"},
        summary="Routing tracing for Werkzeugish",
    )
    _install(
        site,
        distribution="Flaskish",
        version="3.1.0",
        entries={},
        top_level=("cfgt_flaskish",),
    )

    monkeypatch.syspath_prepend(str(site))
    return site


def _run(*argv: str, capsys: pytest.CaptureFixture[str]) -> str:
    command_main(list(argv))
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_options_are_parsed() -> None:
    options = _parse(["--config", "x.toml", "-v", "--toml", "--enabled"])

    assert options.config == "x.toml"
    assert options.verbose is True
    assert options.toml is True
    assert options.enabled is True

    assert _parse(["--config=y.toml"]).config == "y.toml"


def test_enabled_requires_toml() -> None:
    with pytest.raises(SystemExit) as info:
        _parse(["--enabled"])

    assert info.value.code == 2


def test_unknown_options_and_stray_arguments_are_usage_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as info:
        _parse(["--what"])
    assert info.value.code == 2

    with pytest.raises(SystemExit) as info:
        _parse(["stray"])
    assert info.value.code == 2

    with pytest.raises(SystemExit) as info:
        _parse(["--config"])
    assert info.value.code == 2
    assert "--config requires a value" in capsys.readouterr().err


def test_help_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        _parse(["-h"])

    assert info.value.code == 0
    assert "usage: python -m wrapture.tools instrumentation" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the listing
# ---------------------------------------------------------------------------


def test_the_listing_describes_each_installed_instrumentation(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(capsys=capsys)

    expected = textwrap.dedent(
        """\
        flaskish  (wrapture-instrumentation-flaskish 1.2.0)
          Request, view and blueprint tracing for Flaskish applications.
          target: cfgt_flaskish 3.1.0, supported (>=2.0,<4)
          modules: cfgt_flaskish.app, cfgt_flaskish.sansio (>=3.5)
          requires: cfgt_werkzeugish
          removable: yes
          settings:
            capture_headers = false   record request headers
            ignore_paths = []         paths never traced, exact match
            timeout = ...             seconds before a slow view is flagged

        werkzeugish  (wrapture-instrumentation-werkzeugish 0.3)
          Routing tracing for Werkzeugish
          target: cfgt_werkzeugish, not installed
          modules: cfgt_werkzeugish.routing
          removable: no
          settings: (none)
        """
    )
    assert out == expected


def test_verbose_shows_the_dry_run_and_the_url(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run("--verbose", capsys=capsys)

    assert "  would register: cfgt_flaskish.app\n" in out
    assert (
        "  would skip: cfgt_flaskish.sansio (target 3.1.0 is outside '>=3.5')\n" in out
    )
    assert "  url: https://example.test/flaskish\n" in out


def test_nothing_installed_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run(capsys=capsys)

    assert out == "no instrumentation is installed or named by the config\n"


def test_a_config_marks_selected_entries_and_lists_local_ones(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local = tmp_path / "wrapture_local"
    local.mkdir()
    (local / "__init__.py").write_text("")
    (local / "hooks.py").write_text(
        textwrap.dedent(
            '''
            import wrapture

            class ShopInstrumentation(wrapture.Instrumentation):
                """Observe gateway charges over a threshold."""

                target = "cfgt_shop"
                removable = True
                settings = {"threshold": wrapture.Setting(100, "the cutoff")}

                @wrapture.instrumentation_hook("cfgt_shop")
                def shop(self, name, module):
                    pass
            '''
        )
    )
    config = tmp_path / "wrapture.toml"
    config.write_text(
        textwrap.dedent(
            """
            pythonpath = "."

            [[instrument]]
            name = "flaskish"
            enabled = false

            [[instrument]]
            name = "wrapture_local.hooks:ShopInstrumentation"
            threshold = 400
            """
        )
    )

    out = _run("--config", str(config), capsys=capsys)

    assert f"  config: disabled in {config}\n" in out.split("\nwerkzeugish  (")[0]
    assert (
        "cfgt_shop  (local: wrapture_local.hooks:ShopInstrumentation)\n"
        "  Observe gateway charges over a threshold.\n"
        "  target: cfgt_shop, not installed\n"
        "  modules: cfgt_shop\n"
        "  removable: yes\n"
        "  settings:\n"
        "    threshold = 100   the cutoff\n"
        f"  config: enabled in {config}\n"
    ) in out


def test_a_bad_config_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "wrapture.toml"
    config.write_text("[[instrument]]\nthreshold = 1\n")

    with pytest.raises(SystemExit) as info:
        command_main(["--config", str(config)])

    assert info.value.code == 1
    assert "name key is required" in capsys.readouterr().err


def test_a_class_that_cannot_load_is_listed_with_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    _install(
        site,
        distribution="wrapture-instrumentation-broken",
        version="0.1",
        entries={"broken": "cfgt_nowhere:Broken"},
    )
    monkeypatch.syspath_prepend(str(site))

    out = _run(capsys=capsys)

    assert out.startswith("broken  (wrapture-instrumentation-broken 0.1)\n  error: ")
    assert "cfgt_nowhere" in out


def test_a_name_two_distributions_register_is_shown_qualified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    for suffix in ("a", "b"):
        (site / f"cfgt_dup_{suffix}.py").write_text(
            textwrap.dedent(
                """
                import wrapture

                class Requestsish(wrapture.Instrumentation):
                    target = "cfgt_requestsish"
                    removable = True

                    @wrapture.instrumentation_hook("cfgt_requestsish")
                    def requestsish(self, name, module):
                        pass
                """
            )
        )
        _install(
            site,
            distribution=f"wrapture-instrumentation-{suffix}",
            version="0.1",
            entries={"requestsish": f"cfgt_dup_{suffix}:Requestsish"},
        )
    monkeypatch.syspath_prepend(str(site))

    out = _run(capsys=capsys)
    assert (
        "requestsish@wrapture-instrumentation-a  (wrapture-instrumentation-a 0.1)"
        in out
    )
    assert (
        "requestsish@wrapture-instrumentation-b  (wrapture-instrumentation-b 0.1)"
        in out
    )

    toml = _run("--toml", capsys=capsys)
    assert 'name = "requestsish@wrapture-instrumentation-a"\n' in toml
    assert "also provided by wrapture-instrumentation-b, enable one" in toml


def test_mixed_removability_is_spelled_out_per_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "cfgt_pkg_mixed.py").write_text(
        textwrap.dedent(
            """
            import wrapture

            class Mixed(wrapture.Instrumentation):
                target = "cfgt_mixed"
                removable = True

                @wrapture.instrumentation_hook("cfgt_mixed")
                def undoable(self, name, module):
                    pass

                @wrapture.instrumentation_hook("cfgt_mixed.sticky", removable=False)
                def sticky(self, name, module):
                    pass
            """
        )
    )
    _install(
        site,
        distribution="wrapture-instrumentation-mixed",
        version="0.1",
        entries={"mixed": "cfgt_pkg_mixed:Mixed"},
    )
    monkeypatch.syspath_prepend(str(site))

    out = _run(capsys=capsys)

    assert "  removable: cfgt_mixed only, not cfgt_mixed.sticky\n" in out


# ---------------------------------------------------------------------------
# the TOML template
# ---------------------------------------------------------------------------


def test_toml_writes_a_disabled_template_per_instrumentation(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run("--toml", capsys=capsys)

    expected = textwrap.dedent(
        """\
        # flaskish@wrapture-instrumentation-flaskish 1.2.0
        # Request, view and blueprint tracing for Flaskish applications.
        # target cfgt_flaskish 3.1.0, supported (>=2.0,<4); requires cfgt_werkzeugish
        [[instrument]]
        name = "flaskish"
        enabled = false
        # capture_headers = false   # record request headers
        # ignore_paths = []         # paths never traced, exact match
        # timeout = ...             # (no default) seconds before a slow view is flagged

        # werkzeugish@wrapture-instrumentation-werkzeugish 0.3
        # Routing tracing for Werkzeugish
        # target cfgt_werkzeugish, not installed
        [[instrument]]
        name = "werkzeugish"
        enabled = false
        """
    )
    assert out == expected


def test_toml_enabled_emits_the_entries_live(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run("--toml", "--enabled", capsys=capsys)

    assert "enabled = false" not in out
    assert 'name = "flaskish"\n# capture_headers' in out


def test_toml_with_a_config_emits_only_what_the_file_lacks(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "wrapture.toml"
    config.write_text('[[instrument]]\nname = "flaskish"\n')

    out = _run("--toml", "--config", str(config), capsys=capsys)

    assert "flaskish@" not in out
    assert 'name = "werkzeugish"\n' in out

    config.write_text(
        '[[instrument]]\nname = "flaskish"\n\n[[instrument]]\nname = "werkzeugish"\n'
    )

    out = _run("--toml", "--config", str(config), capsys=capsys)
    assert out == "# no instrumentation to add\n"


def test_the_template_loads_as_a_config(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from wrapture import load_config

    out = _run("--toml", capsys=capsys)
    config = tmp_path / "generated.toml"
    config.write_text(out)

    loaded = load_config(config)

    assert [entry.enabled for entry in loaded.instrument] == [False, False]


# ---------------------------------------------------------------------------
# the dispatcher and the -m wiring
# ---------------------------------------------------------------------------


def test_the_dispatcher_routes_to_the_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tools_main(["instrumentation"])

    assert "no instrumentation is installed" in capsys.readouterr().out


def test_the_dispatcher_lists_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        tools_main([])

    assert "instrumentation" in capsys.readouterr().out


def test_the_module_is_runnable() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "wrapture.tools", "instrumentation", "--toml"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("#")


def test_verbose_names_the_interpreter_as_a_standard_library_targets_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "cfgt_pkg_jsonish.py").write_text(
        textwrap.dedent(
            """
            import wrapture

            class JsonishInstrumentation(wrapture.Instrumentation):
                target = "json"
                supports = ">=3.12"

                @wrapture.instrumentation_hook("json")
                def json(self, name, module):
                    pass
            """
        )
    )
    _install(
        site,
        distribution="wrapture-instrumentation-jsonish",
        version="0.1",
        entries={"jsonish": "cfgt_pkg_jsonish:JsonishInstrumentation"},
    )
    monkeypatch.syspath_prepend(str(site))

    out = _run("--verbose", capsys=capsys)

    assert (
        f"  target: json (standard library, python {platform.python_version()}),"
        " supported (>=3.12)\n"
    ) in out
    assert "  would register: json\n" in out
