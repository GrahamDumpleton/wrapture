"""Windows: named spans of time during which sinks listen and collectors
collect, on a schedule or on demand, each run ending in a report.

A Window holds contents (sinks, and collectors satisfying the Collector
protocol) and owns time: an opening trigger, a duration, an optional
repeat. Opening a run registers the window's sinks as process sinks
and arms its collectors; closing it deregisters and disarms them,
asks each collector for a Report, and delivers the reports to their
destinations. Between runs a window's contents hear nothing, so a
binding whose only listener is a closed window is on the fast path.

Triggers, all optional and combinable within the rules given on
Window: `after` opens once, that long after start(); `every` repeats;
`times` caps the repeats; `align` puts the repeats on the local
wall-clock boundary; `at` opens the first run at the next local
occurrence of a time of day. `duration` is how long a run stays open,
with two shortcuts: no trigger and no duration is one run for the
whole process; `every` without duration is back-to-back runs, each
the whole period. A window's runs never overlap: an open while a run
is in progress is refused and counted.

Reports go to up to three places: retained on the window (the last
`retain`, default 10) and on the Run object the context manager form
yields; passed to `on_report`, a plain callable, errors suppressed
and counted as a sink's would be; and written under `report`, an
output path template, one file per run per collector, temp-then-
rename so a half-written report is never observed.

Wall-clock triggers (`at`, aligned `every`) are recomputed from the
local clock after each run, so they follow daylight saving as a wall
clock does; relative triggers (`after`, unaligned `every`) and
`duration` are monotonic durations. Schedules live in the process and
start afresh at start(); nothing is persisted or resumed.
"""

from __future__ import annotations

import atexit
import os
import random
import threading
import time
import warnings
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .exceptions import ConfigWarning
from .outputs import OutputPath, open_output
from .scheduler import (
    Schedule,
    _next_aligned,
    _next_at,
    _parse_hhmm,
    once,
    parse_duration,
)
from .sinks import Sink, _note_sink_error, add_sink, remove_sink

__all__ = ["Collector", "Report", "Run", "Window", "window"]


@dataclass(frozen=True)
class Report:
    """What one collector produced for one run of a window.

    The header is common to every kind: `kind` is the collector type,
    `name` the collector's name, `window` and `run` locate the run
    (run numbers count from 1 within the window's schedule),
    `started` and `ended` are aware local datetimes, `duration` is
    seconds, and `cut_short` marks a run closed by stop() or
    interpreter exit before its scheduled end. `text` is the
    human-readable rendering, what the file destination writes;
    `data` is the per-kind payload, documented by each collector, for
    callbacks and tests.
    """

    kind: str
    name: str
    window: str
    run: int
    started: datetime
    ended: datetime
    duration: float
    text: str
    data: Mapping[str, Any] = field(default_factory=dict)
    cut_short: bool = False


@runtime_checkable
class Collector(Protocol):
    """What a window's contents must offer to produce a report.

    arm() starts collecting and disarm() stops, called at run open and
    close; report(run) renders what was collected for that run as a
    Report; reset() clears, so the next run starts fresh. A collector
    that accumulates from events is a Sink as well, and the window
    registers it as one while it is armed.
    """

    def arm(self) -> None: ...

    def disarm(self) -> None: ...

    def report(self, run: Run) -> Report: ...

    def reset(self) -> None: ...


def _stamp(moment: datetime) -> str:
    # The filesystem-safe form of a datetime, matching {datetime}.

    return moment.strftime("%Y-%m-%dT%H-%M-%S")


class Run:
    """One run of a window: its number within the schedule, when it
    opened and closed, and the reports it produced.

    `first` is when the schedule's first run opened, the same for
    every run of one schedule, so a batch of runs can be grouped by
    it; `context` is what the output path variables {window}, {first}
    and {run} expand to for this run.
    """

    def __init__(
        self, window: str, number: int, first: datetime, started: datetime
    ) -> None:
        self.window = window
        self.number = number
        self.first = first
        self.started = started
        self.ended: datetime | None = None
        self.cut_short = False
        self._reports: list[Report] = []

    def __repr__(self) -> str:
        state = "open" if self.ended is None else "closed"
        return f"<Run {self.number} of {self.window!r}, {state}>"

    @property
    def duration(self) -> float | None:
        """Seconds the run has been, or was, open."""

        if self.ended is None:
            return None
        return (self.ended - self.started).total_seconds()

    @property
    def reports(self) -> tuple[Report, ...]:
        """The reports this run produced, one per collector, available
        once the run has closed."""

        return tuple(self._reports)

    @property
    def context(self) -> dict[str, Any]:
        """The values of {window}, {first} and {run} for this run."""

        return {"window": self.window, "first": _stamp(self.first), "run": self.number}


class Window:
    """A named span of time during which sinks listen and collectors
    collect, opened on a schedule or on demand.

    `collect` holds the contents: sinks (registered as process sinks
    while a run is open; a file sink's path may use {window}, {first}
    and {run}, and its file is opened per run and released between
    runs) and collectors (armed while a run is open, each producing a
    Report at close). `duration` is how long a run stays open; `after`,
    `every`, `times`, `align`, `at` and `jitter` are the triggers
    described in the module docstring, all durations in the forms
    rotate= accepts ("30s", "1h", or seconds) and `at` a local "HH:MM".
    `at` cannot be combined with `after` or `align`; `times` and `align`
    need `every`; `duration` must be shorter than `every`.

    `report` is an output path template for the file destination,
    `on_report` a callable given each Report, and `retain` how many
    reports the window keeps on itself.

    start() arms the schedule and stop() takes it down, closing an open
    run; open() and close() drive runs by hand between them. Config
    windows are started by apply() and stopped by revert(); an
    interpreter exit closes any open run and delivers its reports.
    """

    def __init__(
        self,
        *,
        name: str = "window",
        after: str | int | float | None = None,
        duration: str | int | float | None = None,
        every: str | int | float | None = None,
        times: int | None = None,
        align: bool = False,
        at: str | None = None,
        jitter: str | int | float | None = None,
        collect: Iterable[Any] = (),
        report: str | os.PathLike[str] | None = None,
        on_report: Callable[[Report], Any] | None = None,
        retain: int = 10,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("window name must be a non-empty string")

        self._name = name
        self._after = parse_duration(after) if after is not None else None
        self._duration = parse_duration(duration) if duration is not None else None
        self._every = parse_duration(every) if every is not None else None
        self._jitter = parse_duration(jitter) if jitter is not None else None
        self._align = bool(align)
        self._at = at

        if at is not None:
            _parse_hhmm(at)
            if self._after is not None or self._align:
                raise ValueError("at cannot be combined with after or align")

        if times is not None:
            if not isinstance(times, int) or isinstance(times, bool) or times < 1:
                raise ValueError(f"times must be a positive integer, got {times!r}")
            if self._every is None:
                raise ValueError("times needs every")
        self._times = times

        if self._align and self._every is None:
            raise ValueError("align needs every")

        if (
            self._duration is not None
            and self._every is not None
            and self._duration >= self._every
        ):
            raise ValueError("duration must be shorter than every; runs never overlap")

        if not isinstance(retain, int) or retain < 0:
            raise ValueError(f"retain must be a non-negative integer, got {retain!r}")

        # Sort the contents: a collector satisfies the protocol (and may
        # be a sink as well, registered while armed); anything else
        # must be a plain sink.

        self._collectors: list[Collector] = []
        self._sinks: list[Sink] = []

        for item in collect:
            if isinstance(item, Collector):
                self._collectors.append(item)
            elif isinstance(item, Sink):
                self._sinks.append(item)
            else:
                raise TypeError(
                    f"window {name!r} contents must be sinks or collectors,"
                    f" got {item!r}"
                )

        if on_report is not None and not callable(on_report):
            raise TypeError(f"on_report must be callable, got {on_report!r}")
        self._on_report = on_report

        self._report_path = (
            OutputPath(report, name=name) if report is not None else None
        )

        if (report is not None or on_report is not None) and not self._collectors:
            warnings.warn(
                f"window {name!r} has a report destination but no collectors,"
                f" so it will never produce a report",
                ConfigWarning,
                stacklevel=2,
            )

        # A repeating window whose file sink names one file for every
        # run rewrites it each time, which is almost certainly not the
        # intent, so it is warned about here where the path is given.

        if self._every is not None:
            for sink in self._sinks:
                path = getattr(sink, "_path", None)
                if isinstance(path, OutputPath) and not (path.windowed or path.timed):
                    warnings.warn(
                        f"window {name!r} repeats but the path"
                        f" {path.template!r} names the same file every run;"
                        f" add {{run}}, {{first}} or a time variable so each"
                        f" run gets its own file",
                        ConfigWarning,
                        stacklevel=2,
                    )

        self._lock = threading.RLock()
        self._run: Run | None = None
        self._first: datetime | None = None
        self._last_day: tuple[int, int, int] | None = None
        self._count = 0
        self._started = False
        self._stopped = False
        self._refused = 0
        self._reports: deque[Report] = deque(maxlen=retain)
        self._open_schedule: Schedule | None = None
        self._close_schedule: Schedule | None = None
        self.errors = 0

    def __repr__(self) -> str:
        parts = [f"Window({self._name!r}"]
        for key in ("after", "duration", "every", "times", "at"):
            value = getattr(self, f"_{key}")
            if value is not None:
                parts.append(f", {key}={value!r}")
        if self._align:
            parts.append(", align=True")
        if self._report_path is not None:
            parts.append(f", report={self._report_path.template!r}")
        return "".join(parts) + ")"

    # -- inspection ---------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def run(self) -> Run | None:
        """The run in progress, or None between runs."""

        with self._lock:
            return self._run

    @property
    def runs(self) -> int:
        """How many runs have opened so far."""

        with self._lock:
            return self._count

    @property
    def refused(self) -> int:
        """How many opens were refused because a run was in progress."""

        with self._lock:
            return self._refused

    @property
    def reports(self) -> tuple[Report, ...]:
        """The most recent reports, oldest first, up to `retain`."""

        with self._lock:
            return tuple(self._reports)

    @property
    def collectors(self) -> tuple[Collector, ...]:
        return tuple(self._collectors)

    @property
    def sinks(self) -> tuple[Sink, ...]:
        return tuple(self._sinks)

    @property
    def report_path(self) -> str | None:
        """The report file template, or None."""

        return self._report_path.template if self._report_path is not None else None

    def describe(self) -> str:
        """One line saying when the window runs, for config.report()."""

        clauses: list[str] = []
        if self._at is not None:
            clauses.append(f"at {self._at}")
        if self._after is not None:
            clauses.append(f"after {self._after:g}s")
        if self._every is not None:
            cadence = f"every {self._every:g}s"
            if self._align:
                cadence += " aligned"
            if self._times is not None:
                cadence += f" x{self._times}"
            clauses.append(cadence)
        if self._duration is not None:
            clauses.append(f"for {self._duration:g}s")
        elif self._every is not None:
            clauses.append("back to back")
        elif not clauses:
            clauses.append("whole process")

        return ", ".join(clauses)

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Arm the schedule: open the first run now or at its trigger,
        and repeat as configured. Idempotent."""

        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopped = False

        _register(self)

        delay = self._first_delay()
        if delay <= 0:
            self.open()
        else:
            self._schedule_open(delay)

    def stop(self) -> None:
        """Cancel the schedule and close an open run, marking it cut
        short when it had a scheduled end. Idempotent."""

        with self._lock:
            if not self._started or self._stopped:
                return
            self._stopped = True

            if self._open_schedule is not None:
                self._open_schedule.cancel()
                self._open_schedule = None

            planned = self._duration is not None or self._every is not None

        self.close(cut_short=planned)

    def open(self) -> bool:
        """Open a run now. Returns False, and counts the refusal, when a
        run is already open or the schedule has run its course."""

        with self._lock:
            if self._run is not None:
                self._refused += 1
                return False

            if self._stopped:
                return False

            if self._times is not None and self._count >= self._times:
                return False

            now = datetime.now().astimezone()
            if self._first is None:
                self._first = now
            self._last_day = (now.year, now.month, now.day)

            self._count += 1
            run = Run(self._name, self._count, self._first, now)
            self._run = run

            # Contents come alive: file sinks learn the run so their
            # paths expand to it, then everything is registered and
            # armed. A collector that is also a sink registers once.

            for sink in self._sinks:
                _bind_context(sink, run.context)
                add_sink(sink)

            for collector in self._collectors:
                if isinstance(collector, Sink):
                    _bind_context(collector, run.context)
                    add_sink(collector)
                try:
                    collector.arm()
                except Exception:
                    self._note_error(collector)

            # The run's end, and the next run's start.

            if self._duration is not None:
                self._close_schedule = once(
                    self._scheduled_close,
                    self._duration,
                    name=f"window {self._name!r} close",
                )

            if self._every is not None and not (
                self._times is not None and self._count >= self._times
            ):
                self._schedule_open(self._next_delay())

        return True

    def close(self, *, cut_short: bool = False) -> Run | None:
        """Close the open run, deliver its reports, and return it; None
        when no run is open."""

        with self._lock:
            run = self._run
            if run is None:
                return None

            self._run = None
            if self._close_schedule is not None:
                self._close_schedule.cancel()
                self._close_schedule = None

            run.ended = datetime.now().astimezone()
            run.cut_short = cut_short

            for collector in self._collectors:
                try:
                    collector.disarm()
                except Exception:
                    self._note_error(collector)
                if isinstance(collector, Sink):
                    remove_sink(collector)

            for sink in self._sinks:
                remove_sink(sink)
                _release(sink)

            reports: list[Report] = []
            for collector in self._collectors:
                try:
                    reports.append(collector.report(run))
                except Exception:
                    self._note_error(collector)
                try:
                    collector.reset()
                except Exception:
                    self._note_error(collector)

            run._reports = reports
            self._reports.extend(reports)

        # Delivery runs outside the lock: the callback and the file
        # write are user-facing and must not hold up open().

        for report in reports:
            self._deliver(report, run, several=len(reports) > 1)

        return run

    # -- internals ----------------------------------------------------

    def _note_error(self, source: Any) -> None:
        self.errors += 1
        _note_sink_error(source)

    def _first_delay(self) -> float:
        # Seconds until the first run should open.

        now = time.time()

        if self._at is not None:
            delay = _next_at(self._at, now) - now
        elif self._align:
            assert self._every is not None
            base = now + (self._after or 0.0)
            delay = _next_aligned(self._every, base) - now
        elif self._after is not None:
            delay = self._after
        else:
            delay = 0.0

        return delay + self._jittered()

    def _next_delay(self) -> float:
        # Seconds from now until the next repeat opens: on the local
        # grid when aligned, from the `at` anchor when anchored, else a
        # plain interval from this open.

        assert self._every is not None
        now = time.time()

        if self._align:
            delay = _next_aligned(self._every, now) - now
            if delay < 1.0:
                delay += self._every
        elif self._at is not None:
            if self._every % 86400 == 0:
                delay = _next_at(self._at, now, skip_day=self._last_day) - now
            else:
                assert self._first is not None
                anchor = self._first.timestamp()
                elapsed = now - anchor
                steps = int(elapsed // self._every) + 1
                delay = anchor + steps * self._every - now
        else:
            delay = self._every

        return delay + self._jittered()

    def _jittered(self) -> float:
        if self._jitter is None:
            return 0.0
        return random.uniform(0.0, self._jitter)

    def _schedule_open(self, delay: float) -> None:
        with self._lock:
            if self._stopped:
                return
            self._open_schedule = once(
                self._scheduled_open, delay, name=f"window {self._name!r} open"
            )

    def _scheduled_open(self) -> None:
        # A repeat with no duration is back to back: the previous run
        # ends as this one begins.

        with self._lock:
            self._open_schedule = None
            if self._stopped:
                return

        if self._duration is None and self._every is not None:
            self.close()

        self.open()

    def _scheduled_close(self) -> None:
        with self._lock:
            self._close_schedule = None
        self.close()

    def _deliver(self, report: Report, run: Run, *, several: bool) -> None:
        if self._on_report is not None:
            try:
                self._on_report(report)
            except Exception:
                self._note_error(self._on_report)

        if self._report_path is not None:
            try:
                self._write_report(report, run, several)
            except Exception:
                self._note_error(self._report_path)

    def _write_report(self, report: Report, run: Run, several: bool) -> None:
        # Whole-file write at run close, temp then rename, so a reader
        # never sees a partial report. With several collectors each
        # report gets the collector's name before the extension.

        assert self._report_path is not None
        path = self._report_path.expand(window=run.context)

        if several:
            stem, extension = os.path.splitext(path)
            path = f"{stem}-{report.name}{extension}"

        temporary = f"{path}.tmp"
        with open_output(temporary, "w") as stream:
            stream.write(report.text)
            if not report.text.endswith("\n"):
                stream.write("\n")

        os.replace(temporary, path)


def _bind_context(sink: Any, context: Mapping[str, Any]) -> None:
    # A file sink carries its OutputPath as _path; the run's context
    # is set on it so the sink's own opens expand the window variables
    # without the sink knowing about windows.

    path = getattr(sink, "_path", None)
    if isinstance(path, OutputPath):
        path.context = dict(context)


def _release(sink: Any) -> None:
    # Between runs a file sink holds no file: flush what the run wrote
    # (so a run closed at interpreter exit still lands on disk) and let
    # the file go.

    try:
        sink.flush()
        release = getattr(sink, "release", None)
        if callable(release):
            release()
    except Exception:
        _note_sink_error(sink)


# Every started window is registered so interpreter exit can close an
# open run and deliver its reports; the hook is installed once, on the
# first start.

_windows: weakref.WeakSet[Window] = weakref.WeakSet()
_registry_lock = threading.Lock()
_hooked = False


def _register(window: Window) -> None:
    global _hooked

    with _registry_lock:
        _windows.add(window)
        if not _hooked:
            _hooked = True
            atexit.register(_shutdown_windows)


def _shutdown_windows() -> None:
    with _registry_lock:
        windows = list(_windows)

    for window in windows:
        try:
            window.stop()
        except Exception:
            _note_sink_error(window)


class _WindowScope:
    # The context manager behind window(): a window with no triggers,
    # opened on enter and stopped on exit, yielding the run.

    def __init__(self, window: Window) -> None:
        self._window = window
        self._run: Run | None = None

    def __enter__(self) -> Run:
        self._window.start()
        run = self._window.run
        if run is None:
            raise RuntimeError("window did not open")
        self._run = run
        return run

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._window.stop()


def window(
    *,
    collect: Sequence[Any] = (),
    duration: str | int | float | None = None,
    name: str = "window",
    report: str | os.PathLike[str] | None = None,
    on_report: Callable[[Report], Any] | None = None,
) -> _WindowScope:
    """A window as a context manager, the sibling of timeline(): the
    run opens on entry, closes on exit (or after `duration`, if given
    and shorter), and is yielded so its reports can be read afterwards.

        with wrapture.window(collect=[wrapture.Aggregate()]) as run:
            drive_traffic()
        print(run.reports[0].text)
    """

    return _WindowScope(
        Window(
            name=name,
            duration=duration,
            collect=collect,
            report=report,
            on_report=on_report,
        )
    )
