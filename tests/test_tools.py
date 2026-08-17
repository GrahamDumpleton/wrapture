"""Tests for the wrapture.tools command line: the dispatcher and the
convert command.

python -m wrapture.tools lists the commands; convert renders a
JSONLines trace file with one of the exporters, to a file or to
standard output. Parsing and behaviour are tested in process; one
subprocess test proves the -m wiring end to end.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from wrapture.tools.__main__ import main as tools_main
from wrapture.tools.convert import _parse
from wrapture.tools.convert import main as convert_main

# A small nested trace in the serialised form, written in completion
# order the way a JSONLines file would hold it.

RECORDS: list[dict[str, Any]] = [
    {
        "seq": 2,
        "parent_id": 1,
        "depth": 1,
        "kind": "call",
        "path": "app:Gateway.charge",
        "started": 0.001,
        "duration": 0.002,
        "thread_id": 7,
        "thread_name": "MainThread",
        "result": "ch_500",
    },
    {
        "seq": 1,
        "parent_id": None,
        "depth": 0,
        "kind": "call",
        "path": "app:Processor.process",
        "started": 0.0,
        "duration": 0.01,
        "thread_id": 7,
        "thread_name": "MainThread",
    },
]


@pytest.fixture
def trace(tmp_path: Path) -> Path:
    source = tmp_path / "trace.jsonl"
    source.write_text("".join(json.dumps(record) + "\n" for record in RECORDS))
    return source


# ---------------------------------------------------------------------------
# the dispatcher
# ---------------------------------------------------------------------------


def test_no_command_lists_the_available_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        tools_main([])

    assert caught.value.code == 0
    assert "convert" in capsys.readouterr().out


def test_an_unknown_command_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        tools_main(["transmogrify"])

    assert caught.value.code == 2
    assert "unknown command" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# convert parsing
# ---------------------------------------------------------------------------


def test_options_and_the_trace_file_are_accepted_in_any_order() -> None:
    for argv in (
        ["--format", "chrome", "-o", "out.json", "trace.jsonl"],
        ["trace.jsonl", "--format=chrome", "--output=out.json"],
        ["--output", "out.json", "trace.jsonl", "--format", "chrome"],
    ):
        conversion = _parse(argv)

        assert conversion.format == "chrome"
        assert conversion.output == "out.json"
        assert conversion.source == "trace.jsonl"


def test_the_format_is_required() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["trace.jsonl"])

    assert caught.value.code == 2


def test_an_unknown_format_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--format", "interpretive-dance", "trace.jsonl"])

    assert caught.value.code == 2


def test_the_trace_file_is_required() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--format", "chrome"])

    assert caught.value.code == 2


def test_a_second_trace_file_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--format", "chrome", "one.jsonl", "two.jsonl"])

    assert caught.value.code == 2


def test_an_unknown_option_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--verbose", "trace.jsonl"])

    assert caught.value.code == 2


def test_help_prints_usage_and_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        _parse(["--help"])

    assert caught.value.code == 0
    assert "usage: python -m wrapture.tools convert" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# convert behaviour
# ---------------------------------------------------------------------------


def test_convert_renders_to_standard_output_by_default(
    trace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    convert_main(["--format", "canonical", str(trace)])

    assert capsys.readouterr().out == (
        "call app:Processor.process\n  call app:Gateway.charge\n"
    )


def test_convert_writes_the_output_file(trace: Path, tmp_path: Path) -> None:
    output = tmp_path / "trace.json"

    convert_main(["--format", "chrome", "-o", str(output), str(trace)])

    rendered = json.loads(output.read_text())
    slices = [entry for entry in rendered["traceEvents"] if entry["ph"] == "X"]
    assert {entry["name"] for entry in slices} == {
        "app:Processor.process",
        "app:Gateway.charge",
    }


def test_convert_renders_mermaid(
    trace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    convert_main(["--format", "mermaid", str(trace)])

    out = capsys.readouterr().out
    assert out.startswith("sequenceDiagram\n")
    assert "    P1->>+P2: charge" in out


def test_a_missing_trace_file_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        convert_main(["--format", "chrome", str(tmp_path / "absent.jsonl")])

    assert caught.value.code == 1
    assert "cannot read trace file" in capsys.readouterr().err


def test_a_file_that_is_not_jsonlines_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "trace.jsonl"
    source.write_text("this is not json\n")

    with pytest.raises(SystemExit) as caught:
        convert_main(["--format", "chrome", str(source)])

    assert caught.value.code == 1
    assert "not a JSONLines trace" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def test_the_module_is_runnable(trace: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wrapture.tools",
            "convert",
            "--format",
            "canonical",
            trace.name,
        ],
        cwd=trace.parent,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (
        "call app:Processor.process\n  call app:Gateway.charge\n"
    )
