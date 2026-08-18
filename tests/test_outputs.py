"""Tests for templated output paths and the scheduler behind rotation."""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from wrapture.outputs import OutputPath, open_output
from wrapture.scheduler import _next_aligned, every, parse_duration

# ---------------------------------------------------------------------------
# path templates
# ---------------------------------------------------------------------------


def test_a_plain_path_expands_to_itself() -> None:
    path = OutputPath("logs/trace.jsonl", name="jsonlines")

    assert path.expand() == "logs/trace.jsonl"
    assert path.variables == frozenset()
    assert not path.timed


def test_the_time_variables_expand_in_local_time() -> None:
    when = 1_700_000_000.0
    local = time.localtime(when)
    path = OutputPath(
        "{date}/{time}/{datetime}/{epoch}/{now:%Y%m%d-%H}/{utc:%H}", name="x"
    )

    assert path.expand(when=when) == "/".join(
        [
            time.strftime("%Y-%m-%d", local),
            time.strftime("%H-%M-%S", local),
            time.strftime("%Y-%m-%dT%H-%M-%S", local),
            "1700000000",
            time.strftime("%Y%m%d-%H", local),
            time.strftime("%H", time.gmtime(when)),
        ]
    )
    assert path.timed


def test_the_identity_variables_expand() -> None:
    path = OutputPath("{host}-{pid}-{name}", name="ops")

    assert path.expand() == f"{socket.gethostname()}-{os.getpid()}-ops"


def test_values_cannot_escape_the_directory() -> None:
    # A name or a strftime format that would introduce separators or
    # climb a directory is flattened into one safe component.

    assert OutputPath("out/{name}.log", name="../etc/x").expand() == "out/_-etc-x.log"
    assert OutputPath("out/{name}.log", name="..").expand() == "out/_.log"
    assert OutputPath("out/{now:%Y/%m}.log", name="x").expand(
        when=1_700_000_000.0
    ) == "out/{}.log".format(time.strftime("%Y-%m", time.localtime(1_700_000_000.0)))


def test_window_variables_need_a_window() -> None:
    path = OutputPath("{window}-{first}-{run:03}.txt", name="x")

    assert path.windowed

    with pytest.raises(ValueError, match="only have a value inside a window"):
        path.expand()

    assert (
        path.expand(window={"window": "hourly", "first": "2026-08-18", "run": 7})
        == "hourly-2026-08-18-007.txt"
    )

    # The window sets a context on the path so the sink's own opens
    # pick it up without knowing about windows.

    path.context = {"window": "hourly", "first": "2026-08-18", "run": 8}

    assert path.expand() == "hourly-2026-08-18-008.txt"


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{seq}", "unknown variable {seq}"),
        ("{now}", "needs a strftime format"),
        ("{pid:04}", "takes no format specification"),
        ("{name!r}", "conversions such as !r"),
        ("{run:x}", "integer width"),
    ],
)
def test_bad_templates_fail_at_construction(template: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        OutputPath(template, name="x")


def test_open_output_creates_the_parent_directories(tmp_path: os.PathLike[str]) -> None:
    target = os.path.join(tmp_path, "a", "b", "c.log")

    with open_output(target, "a") as stream:
        stream.write("x\n")

    with open(target, encoding="utf-8") as stream:
        assert stream.read() == "x\n"


# ---------------------------------------------------------------------------
# durations and the scheduler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("30s", 30.0), ("15m", 900.0), ("1h", 3600.0), ("2d", 172800.0)],
)
def test_durations_parse(value: str, seconds: float) -> None:
    assert parse_duration(value) == seconds


def test_compound_and_numeric_durations() -> None:
    assert parse_duration("1h30m") == 5400.0
    assert parse_duration("1.5h") == 5400.0
    assert parse_duration(90) == 90.0
    assert parse_duration(0.25) == 0.25


@pytest.mark.parametrize("value", ["", "soon", "10", "1x", "-1h", 0, -5, True])
def test_bad_durations_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)  # type: ignore[arg-type]


def test_aligned_boundaries_sit_on_the_local_grid() -> None:
    # Hourly aligns to the top of the next local hour, daily to the
    # next local midnight, whatever the moment within the period.

    now = time.mktime((2026, 3, 10, 14, 5, 30, 0, 0, -1))

    assert time.localtime(_next_aligned(3600.0, now))[3:6] == (15, 0, 0)
    assert time.localtime(_next_aligned(86400.0, now))[2:6] == (11, 0, 0, 0)
    assert time.localtime(_next_aligned(900.0, now))[3:6] == (14, 15, 0)

    on_the_hour = time.mktime((2026, 3, 10, 14, 0, 0, 0, 0, -1))

    assert time.localtime(_next_aligned(3600.0, on_the_hour))[3:6] == (15, 0, 0)


def test_the_scheduler_fires_repeatedly_and_stops_on_cancel() -> None:
    fired = threading.Semaphore(0)

    schedule = every(fired.release, 0.02, name="test")

    assert fired.acquire(timeout=5)
    assert fired.acquire(timeout=5)

    schedule.cancel()
    count = schedule.fired

    time.sleep(0.1)

    assert schedule.fired == count
    assert schedule.cancelled


def test_a_failing_callback_warns_and_the_schedule_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The warning is issued on the scheduler thread, where a
    # catch_warnings filter set here would not apply on a
    # free-threaded build, so the call itself is recorded.

    import warnings

    warned: list[str] = []
    monkeypatch.setattr(
        warnings, "warn", lambda message, *args, **kwargs: warned.append(str(message))
    )

    calls = threading.Semaphore(0)

    def explode() -> None:
        calls.release()
        raise RuntimeError("boom")

    schedule = every(explode, 0.02, name="explode")
    try:
        assert calls.acquire(timeout=5)
        assert calls.acquire(timeout=5)
    finally:
        schedule.cancel()

    assert warned and "the schedule continues" in warned[0]
