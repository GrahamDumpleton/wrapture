"""Tests for the python -m wrapture runner.

The runner applies a config and then runs the target as __main__,
with sys.argv rebuilt, the same way python itself would have run it.
Parsing is tested in process; the end-to-end behaviour, including the
patches-before-target-imports ordering guarantee, runs the real
interpreter in a subprocess.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from wrapture.__main__ import _parse, main

# ---------------------------------------------------------------------------
# command line parsing
# ---------------------------------------------------------------------------


def test_everything_after_the_script_belongs_to_the_script() -> None:
    invocation = _parse(["app.py", "--config", "theirs.toml", "-m", "x"])

    assert invocation.script == "app.py"
    assert invocation.config is None
    assert invocation.arguments == ("--config", "theirs.toml", "-m", "x")


def test_everything_after_the_module_belongs_to_the_module() -> None:
    invocation = _parse(["-m", "app", "serve", "--port", "80"])

    assert invocation.module == "app"
    assert invocation.script is None
    assert invocation.arguments == ("serve", "--port", "80")


def test_config_is_accepted_in_both_spellings() -> None:
    assert _parse(["--config", "trace.toml", "app.py"]).config == "trace.toml"
    assert _parse(["--config=trace.toml", "app.py"]).config == "trace.toml"


def test_a_missing_target_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--config", "trace.toml"])

    assert caught.value.code == 2


def test_a_bare_config_option_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--config"])

    assert caught.value.code == 2


def test_a_bare_module_option_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["-m"])

    assert caught.value.code == 2


def test_an_unknown_option_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--verbose", "app.py"])

    assert caught.value.code == 2


def test_help_prints_usage_and_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--help"])

    assert caught.value.code == 0
    assert "usage: python -m wrapture" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# config resolution failures
# ---------------------------------------------------------------------------


def test_finding_no_config_is_an_error_not_a_silent_untraced_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WRAPTURE_CONFIG", raising=False)

    with pytest.raises(SystemExit) as caught:
        main(["app.py"])

    assert caught.value.code == 1
    assert "no config found" in capsys.readouterr().err


def test_a_broken_config_is_reported_not_dumped_as_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "trace.toml"
    source.write_text("not toml at all [")

    with pytest.raises(SystemExit) as caught:
        main(["--config", str(source), "app.py"])

    assert caught.value.code == 1
    assert "wrapture:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def _run(tmp: Path, *arguments: str) -> "subprocess.CompletedProcess[str]":
    environment = dict(os.environ)
    environment.pop("WRAPTURE_CONFIG", None)

    return subprocess.run(
        [sys.executable, "-m", "wrapture", *arguments],
        cwd=tmp,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _write_traced_app(tmp: Path) -> None:
    (tmp / "applib.py").write_text("def parse(text):\n    return text\n")

    # The from-import matters: it only sees the observed function if
    # the config was applied before the target module imported it.

    (tmp / "app.py").write_text(
        textwrap.dedent(
            """
            import sys
            from applib import parse

            parse(sys.argv[1])
            print("ran", sys.argv[1:])
            """
        )
    )

    (tmp / "trace.toml").write_text(
        textwrap.dedent(
            """
            [[observe]]
            target = "applib"
            name = "parse"

            [[sink]]
            type = "jsonlines"
            path = "out.jsonl"
            """
        )
    )


def test_the_runner_applies_the_config_before_the_script_runs(
    tmp_path: Path,
) -> None:
    _write_traced_app(tmp_path)

    completed = _run(tmp_path, "--config", "trace.toml", "app.py", "hello")

    assert completed.returncode == 0, completed.stderr
    assert "ran ['hello']" in completed.stdout

    (line,) = (tmp_path / "out.jsonl").read_text().splitlines()
    record = json.loads(line)
    assert record["path"] == "applib:parse"
    assert record["arguments"] == {"text": "hello"}


def test_the_runner_runs_a_module_target(tmp_path: Path) -> None:
    _write_traced_app(tmp_path)

    completed = _run(tmp_path, "--config", "trace.toml", "-m", "app", "hello")

    assert completed.returncode == 0, completed.stderr
    assert "ran ['hello']" in completed.stdout

    (line,) = (tmp_path / "out.jsonl").read_text().splitlines()
    assert json.loads(line)["path"] == "applib:parse"


def test_a_script_can_import_from_its_own_directory(tmp_path: Path) -> None:
    # python sub/app.py puts sub/ at the front of sys.path; the runner
    # must do the same. The config comes from wrapture.toml in the
    # current directory, exercising the no---config discovery path.

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "helper.py").write_text("VALUE = 'from-helper'\n")
    (sub / "app.py").write_text("import helper\nprint(helper.VALUE)\n")

    (tmp_path / "wrapture.toml").write_text("")

    completed = _run(tmp_path, os.path.join("sub", "app.py"))

    assert completed.returncode == 0, completed.stderr
    assert "from-helper" in completed.stdout
