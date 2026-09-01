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


# ---------------------------------------------------------------------------
# sys.path matches what python itself would have set up
# ---------------------------------------------------------------------------

_PRINT_SYS_PATH = (
    "import json, os, sys\n"
    "print(json.dumps([os.path.realpath(p) if p else p for p in sys.path]))\n"
)


def _sys_path_seen(tmp: Path, *command: str) -> list[str]:
    # The sys.path a target prints when run by `python *command` from
    # tmp, with each entry resolved so the two runs compare like for
    # like. An empty config is present so the runner form has one.

    (tmp / "wrapture.toml").write_text("")

    environment = dict(os.environ)
    environment.pop("WRAPTURE_CONFIG", None)

    completed = subprocess.run(
        [sys.executable, *command],
        cwd=tmp,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    entries: list[str] = json.loads(completed.stdout)

    return entries


def _script_in_subdirectory(tmp: Path) -> str:
    sub = tmp / "sub"
    sub.mkdir()
    (sub / "main.py").write_text(_PRINT_SYS_PATH)

    return os.path.join("sub", "main.py")


def test_a_script_does_not_see_the_launcher_working_directory(
    tmp_path: Path,
) -> None:
    # python -m wrapture inherits the working directory at the front of
    # sys.path, as any -m run does; python sub/main.py has no such
    # entry, so the runner must not leave it behind the script's own
    # directory, where it would change what the script can import.

    script = _script_in_subdirectory(tmp_path)

    native = _sys_path_seen(tmp_path, script)
    wrapped = _sys_path_seen(tmp_path, "-m", "wrapture", script)

    assert native[0] == os.path.realpath(tmp_path / "sub")
    assert os.path.realpath(tmp_path) not in native
    assert wrapped == native


def test_safe_path_mode_adds_nothing_for_a_script(tmp_path: Path) -> None:
    # Under -P python puts neither the script's directory nor the
    # working directory on sys.path; the runner follows suit.

    script = _script_in_subdirectory(tmp_path)

    native = _sys_path_seen(tmp_path, "-P", script)
    wrapped = _sys_path_seen(tmp_path, "-P", "-m", "wrapture", script)

    assert os.path.realpath(tmp_path / "sub") not in native
    assert wrapped == native


def test_a_symlinked_script_resolves_to_its_real_directory(
    tmp_path: Path,
) -> None:
    # On POSIX python follows symlinks when placing a script's
    # directory on sys.path, so a link in the working directory to
    # sub/main.py runs with sub/ at the front, not the directory
    # holding the link. On Windows python resolves nothing, and the
    # runner has to match whichever python does.

    _script_in_subdirectory(tmp_path)

    try:
        (tmp_path / "link.py").symlink_to(tmp_path / "sub" / "main.py")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable here: {exc}")

    native = _sys_path_seen(tmp_path, "link.py")
    wrapped = _sys_path_seen(tmp_path, "-m", "wrapture", "link.py")

    if os.name != "nt":
        assert native[0] == os.path.realpath(tmp_path / "sub")

    assert wrapped == native


def test_a_directory_target_is_placed_on_the_path_itself(tmp_path: Path) -> None:
    # python pkgdir runs pkgdir/__main__.py with pkgdir itself at the
    # front of sys.path, not its parent, and run_path arranges that;
    # the runner must add nothing of its own.

    package = tmp_path / "pkgdir"
    package.mkdir()
    (package / "__main__.py").write_text(_PRINT_SYS_PATH)

    native = _sys_path_seen(tmp_path, "pkgdir")
    wrapped = _sys_path_seen(tmp_path, "-m", "wrapture", "pkgdir")

    assert native[0] == os.path.realpath(package)
    assert wrapped == native


def test_a_module_target_keeps_the_working_directory(tmp_path: Path) -> None:
    # python -m sub.main has the working directory at the front of
    # sys.path, and so does python -m wrapture -m sub.main: the entry
    # the runner inherited is exactly the one python -m would add.

    _script_in_subdirectory(tmp_path)

    native = _sys_path_seen(tmp_path, "-m", "sub.main")
    wrapped = _sys_path_seen(tmp_path, "-m", "wrapture", "-m", "sub.main")

    assert native[0] == os.path.realpath(tmp_path)
    assert wrapped == native


def test_config_pythonpath_entries_precede_the_script_directory(
    tmp_path: Path,
) -> None:
    # The script's directory is settled before the config loads, so
    # the config's pythonpath entries land in front of it, the order
    # the injection path gives when python has already set sys.path.

    script = _script_in_subdirectory(tmp_path)
    (tmp_path / "lib").mkdir()

    wrapped = _sys_path_seen(tmp_path, "-m", "wrapture", script)
    (tmp_path / "wrapture.toml").write_text('pythonpath = ["lib"]\n')

    completed = _run(tmp_path, script)

    assert completed.returncode == 0, completed.stderr
    with_lib = json.loads(completed.stdout)

    assert with_lib[:2] == [
        os.path.realpath(tmp_path / "lib"),
        os.path.realpath(tmp_path / "sub"),
    ]
    assert with_lib[1:] == wrapped
