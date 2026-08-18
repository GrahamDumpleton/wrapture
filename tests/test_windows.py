"""Tests for windows: spans of time during which sinks listen and
collectors collect, on a schedule or on demand, each run ending in a
report delivered to its destinations."""

from __future__ import annotations

import io
import textwrap
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from wrapture import (
    Collector,
    ConfigError,
    ConfigWarning,
    Event,
    JSONLines,
    Printer,
    Report,
    Run,
    Sink,
    SinkErrorWarning,
    Window,
    binding,
    load_config,
    window,
)
from wrapture.scheduler import _next_at
from wrapture.sinks import _process_sinks


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


class Counting(Sink):
    """A collector that is also a sink: counts what it hears while
    armed, and reports the count."""

    capture_args = "none"
    capture_result = "none"

    def __init__(self, name: str = "count") -> None:
        self.name = name
        self.count = 0
        self.armed = False
        self.arms = 0
        self.resets = 0

    def arm(self) -> None:
        self.armed = True
        self.arms += 1

    def disarm(self) -> None:
        self.armed = False

    def report(self, run: Run) -> Report:
        assert run.ended is not None
        return Report(
            kind="count",
            name=self.name,
            window=run.window,
            run=run.number,
            started=run.started,
            ended=run.ended,
            duration=run.duration or 0.0,
            text=f"count {self.count}",
            data={"count": self.count},
            cut_short=run.cut_short,
        )

    def reset(self) -> None:
        self.count = 0
        self.resets += 1

    def on_enter(self, event: Event) -> None:
        self.count += 1


def wait_for(condition: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# the context manager form
# ---------------------------------------------------------------------------


def test_the_context_manager_opens_a_run_and_yields_it() -> None:
    counting = Counting()
    output = io.StringIO()
    charge = binding(Gateway, "charge")

    assert isinstance(counting, Collector)

    with charge:
        Gateway().charge(1)

        with window(collect=[counting, Printer(output, timing=False)]) as run:
            assert counting.armed
            assert counting in _process_sinks
            Gateway().charge(2)
            Gateway().charge(3)

        Gateway().charge(4)

    assert not counting.armed
    assert counting not in _process_sinks
    assert output.getvalue().count("Gateway.charge(") == 2

    (report,) = run.reports
    assert report.kind == "count"
    assert report.data == {"count": 2}
    assert report.run == 1
    assert report.window == "window"
    assert report.cut_short is False
    assert report.started.tzinfo is not None
    assert run.duration is not None and run.duration >= 0

    # The collector was reset for a next run; the report keeps the count.

    assert counting.count == 0
    assert counting.resets == 1


def test_a_window_only_takes_sinks_and_collectors() -> None:
    with pytest.raises(TypeError, match="must be sinks or collectors"):
        Window(collect=[object()])


# ---------------------------------------------------------------------------
# manual open and close, overlap, times
# ---------------------------------------------------------------------------


def test_runs_never_overlap_and_are_numbered() -> None:
    counting = Counting()
    seen: list[Report] = []
    span = Window(name="manual", collect=[counting], on_report=seen.append)

    span.start()
    try:
        assert span.runs == 1  # no trigger: opened immediately
        assert span.open() is False
        assert span.refused == 1

        first = span.close()
        assert first is not None and first.number == 1
        assert span.close() is None

        assert span.open() is True
        assert span.runs == 2
        second = span.close()
        assert second is not None and second.number == 2
        assert second.first == first.first
    finally:
        span.stop()

    assert [report.run for report in seen] == [1, 2]
    assert [report.run for report in span.reports] == [1, 2]


def test_retain_bounds_the_reports_kept() -> None:
    span = Window(name="bounded", collect=[Counting()], retain=2)

    span.start()
    try:
        for _ in range(3):
            span.close()
            span.open()
    finally:
        span.stop()

    assert [report.run for report in span.reports] == [3, 4]


def test_stop_marks_a_scheduled_run_cut_short() -> None:
    span = Window(name="short", duration="1h", collect=[Counting()])

    span.start()
    span.stop()

    (report,) = span.reports
    assert report.cut_short is True

    whole = Window(name="whole", collect=[Counting()])
    whole.start()
    whole.stop()

    (report,) = whole.reports
    assert report.cut_short is False


# ---------------------------------------------------------------------------
# schedules
# ---------------------------------------------------------------------------


def test_every_with_duration_samples_repeatedly_up_to_times() -> None:
    counting = Counting()
    seen: list[Report] = []
    span = Window(
        name="sampled",
        every=0.1,
        duration=0.03,
        times=3,
        collect=[counting],
        on_report=seen.append,
    )
    charge = binding(Gateway, "charge")

    span.start()
    try:
        with charge:
            wait_for(lambda: len(seen) == 3, timeout=10)
    finally:
        span.stop()

    assert [report.run for report in seen] == [1, 2, 3]
    assert all(not report.cut_short for report in seen)
    assert span.runs == 3
    assert counting.arms == 3


def test_every_without_duration_runs_back_to_back() -> None:
    seen: list[Report] = []
    span = Window(
        name="btb", every=0.05, times=3, collect=[Counting()], on_report=seen.append
    )

    span.start()
    try:
        wait_for(lambda: span.runs == 3, timeout=10)
        assert span.run is not None  # the last run stays open: nothing closes it
    finally:
        span.stop()

    assert [report.run for report in span.reports] == [1, 2, 3]
    # Runs 1 and 2 ended by the next opening; only 3 was cut short by stop.
    assert [report.cut_short for report in span.reports] == [False, False, True]


def test_after_delays_the_first_run() -> None:
    span = Window(name="later", after=0.05, collect=[Counting()])

    span.start()
    try:
        assert span.run is None
        wait_for(lambda: span.run is not None)
    finally:
        span.stop()

    assert span.runs == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"times": 3}, "times needs every"),
        ({"align": True}, "align needs every"),
        ({"at": "22:00", "after": "5m"}, "at cannot be combined"),
        ({"at": "22:00", "every": "1h", "align": True}, "at cannot be combined"),
        ({"at": "25:00"}, "out of range"),
        ({"at": "soon"}, "not HH:MM"),
        ({"every": "1h", "duration": "1h"}, "duration must be shorter than every"),
        ({"every": "1h", "times": 0}, "times must be a positive integer"),
        ({"retain": -1}, "retain must be a non-negative integer"),
        ({"name": ""}, "window name must be a non-empty string"),
        ({"after": "soon"}, "not a number with a unit"),
    ],
)
def test_bad_trigger_combinations_are_refused(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Window(**kwargs)


def test_next_at_is_the_next_local_occurrence() -> None:
    now = time.mktime((2026, 3, 10, 14, 5, 30, 0, 0, -1))

    later_today = _next_at("22:00", now)
    assert time.localtime(later_today)[:5] == (2026, 3, 10, 22, 0)

    tomorrow = _next_at("09:30", now)
    assert time.localtime(tomorrow)[:5] == (2026, 3, 11, 9, 30)

    # A day already fired on is skipped even when the time is ahead.

    skipped = _next_at("22:00", now, skip_day=(2026, 3, 10))
    assert time.localtime(skipped)[:5] == (2026, 3, 11, 22, 0)


def test_next_at_follows_daylight_saving() -> None:
    # America/New_York, 2026: clocks go forward on 8 March (02:00 to
    # 03:00 does not exist) and back on 1 November (01:00 to 02:00
    # happens twice). The zone is injected so the test does not depend
    # on the machine's own.

    zone = ZoneInfo("America/New_York")

    before_gap = datetime(2026, 3, 8, 1, 0, tzinfo=zone).timestamp()
    skipped = datetime.fromtimestamp(_next_at("02:30", before_gap, tz=zone), zone)
    assert (skipped.month, skipped.day, skipped.hour, skipped.minute) == (3, 9, 2, 30)

    # After the first 01:30 (EDT) has fired, the second 01:30 (EST) is
    # the same scheduled instant and is not fired again: the candidate
    # for that day is its first occurrence, already past, so the next
    # day's is chosen, with or without the fired day being skipped.

    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone).timestamp()
    for skip in ((2026, 11, 1), None):
        again = datetime.fromtimestamp(
            _next_at("01:30", first + 60, skip_day=skip, tz=zone), zone
        )
        assert (again.month, again.day, again.hour, again.minute) == (11, 2, 1, 30)


# ---------------------------------------------------------------------------
# report destinations
# ---------------------------------------------------------------------------


def test_report_files_are_written_per_run_with_the_window_variables(
    tmp_path: Path,
) -> None:
    span = Window(
        name="stats",
        collect=[Counting()],
        report=tmp_path / "reports" / "{window}-{first}" / "run-{run:02}.txt",
    )

    span.start()
    try:
        span.close()
        span.open()
    finally:
        span.stop()

    (batch,) = (tmp_path / "reports").iterdir()
    assert batch.name.startswith("stats-")
    assert sorted(p.name for p in batch.iterdir()) == ["run-01.txt", "run-02.txt"]
    assert (batch / "run-01.txt").read_text() == "count 0\n"


def test_several_collectors_get_their_name_in_the_report_file(tmp_path: Path) -> None:
    span = Window(
        name="pair",
        collect=[Counting("first"), Counting("second")],
        report=tmp_path / "{window}.txt",
    )

    span.start()
    span.stop()

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "pair-first.txt",
        "pair-second.txt",
    ]
    assert len(span.reports) == 2


def test_an_on_report_error_is_suppressed_and_counted() -> None:
    def explode(report: Report) -> None:
        raise RuntimeError("dashboard down")

    span = Window(name="fragile", collect=[Counting()], on_report=explode)

    with pytest.warns(SinkErrorWarning):
        span.start()
        span.stop()

    assert span.errors == 1
    assert len(span.reports) == 1


def test_a_destination_without_collectors_warns() -> None:
    with pytest.warns(ConfigWarning, match="no collectors"):
        Window(name="empty", report="x.txt", collect=[Printer()])


# ---------------------------------------------------------------------------
# per-run streams
# ---------------------------------------------------------------------------


def test_a_file_sink_inside_a_window_writes_one_file_per_run(tmp_path: Path) -> None:
    printer = Printer(path=tmp_path / "{window}" / "run-{run}.log", timing=False)
    span = Window(name="peek", collect=[printer])
    charge = binding(Gateway, "charge")

    span.start()
    try:
        with charge:
            Gateway().charge(1)
            span.close()
            assert printer.path is None  # released between runs

            span.open()
            Gateway().charge(2)
    finally:
        span.stop()

    first = tmp_path / "peek" / "run-1.log"
    second = tmp_path / "peek" / "run-2.log"

    assert first.read_text().count("Gateway.charge(") == 1
    assert second.read_text().count("Gateway.charge(") == 1


def test_a_jsonlines_sink_inside_a_window_is_released_between_runs(
    tmp_path: Path,
) -> None:
    sink = JSONLines(tmp_path / "trace-{run}.jsonl")
    span = Window(name="jl", collect=[sink])
    charge = binding(Gateway, "charge")

    span.start()
    try:
        with charge:
            Gateway().charge(1)
            span.close()
            span.open()
            Gateway().charge(2)
    finally:
        span.stop()

    sink.close()

    assert (tmp_path / "trace-1.jsonl").read_text().count('"amount":1') == 1
    assert (tmp_path / "trace-2.jsonl").read_text().count('"amount":2') == 1


def test_a_repeating_window_warns_about_a_path_shared_by_every_run(
    tmp_path: Path,
) -> None:
    with pytest.warns(ConfigWarning, match="names the same file every run"):
        Window(name="w", every="1h", collect=[Printer(path=tmp_path / "same.log")])


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_load_config_builds_windows_and_apply_starts_them(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[observe]]
            target = "{__name__}:Gateway"
            name = "charge"

            [[window]]
            name = "peek"
            after = "1h"
            for = "2m"
            every = "3h"
            align = true
            report = "reports/{{window}}-{{run}}.txt"

            [[window.collect]]
            type = "printer"
            path = "peek/{{first}}/run-{{run:02}}.log"
            depth = 1

            [[window]]
            name = "whole"

            [[window.collect]]
            type = "{__name__}:Counting"
            """
        )
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConfigWarning)
        config = load_config(source)

    peek, whole = config.windows
    assert peek.name == "peek"
    assert peek.describe() == "after 3600s, every 10800s aligned, for 120s"
    assert peek.report_path == str(tmp_path / "reports" / "{window}-{run}.txt")
    (depth,) = peek.sinks
    assert type(depth).__name__ == "Depth"
    assert whole.describe() == "whole process"
    (counting,) = whole.collectors
    assert isinstance(counting, Counting)

    applied = config.apply()
    try:
        assert whole.run is not None
        assert peek.run is None
        assert "windows:" in applied.report()
        assert "whole process" in applied.report()

        Gateway().charge(5)
    finally:
        applied.revert()

    assert whole.run is None
    (report,) = whole.reports
    assert report.data == {"count": 1}


def test_a_top_level_sink_cannot_use_window_variables(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text('[[sink]]\ntype = "printer"\npath = "out-{run}.log"\n')

    with pytest.raises(ConfigError, match="only have a value inside a"):
        load_config(source)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[window]\nname = "x"\n', r"write each as a \[\[window\]\] entry"),
        ('[[window]]\nname = "x"\nrepeat = 3\n', "unknown keys"),
        ('[[window]]\nname = "x"\ntimes = 2\n', "times needs every"),
        ('[[window]]\nname = "x"\nalign = "yes"\n', "align must be true or false"),
        ('[[window]]\nname = "x"\ncollect = "printer"\n', "collect must be a list"),
        ('[[window]]\nname = ""\n', "name must be a non-empty string"),
    ],
)
def test_bad_window_tables_fail_at_load(
    tmp_path: Path, body: str, message: str
) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(body)

    with pytest.raises(ConfigError, match=message):
        load_config(source)


def test_a_window_without_a_name_gets_a_positional_one(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text("[[window]]\nafter = '1h'\n")

    (span,) = load_config(source).windows

    assert span.name == "window1"


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------


def test_a_run_hears_events_from_other_threads() -> None:
    counting = Counting()
    charge = binding(Gateway, "charge")

    with charge, window(collect=[counting]) as run:
        worker = threading.Thread(target=lambda: Gateway().charge(9))
        worker.start()
        worker.join()

    assert run.reports[0].data == {"count": 1}
