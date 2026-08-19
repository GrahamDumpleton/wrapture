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
occurrence of a time of day; `on_signal` opens a run when the process
receives a signal and `on_file` when a named file appears (it is
consumed), the two ways an operator opens a run from outside. `duration`
is how long a run stays open,
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

import os
import random
import signal
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
from .lifecycle import WINDOWS, _on_shutdown
from .outputs import OutputPath, open_output
from .scheduler import (
    Schedule,
    _next_aligned,
    _next_at,
    _parse_hhmm,
    every,
    once,
    parse_duration,
)
from .sinks import Depth, Filter, Sample, Sink, _note_sink_error, add_sink, remove_sink

__all__ = ["Collector", "Report", "Run", "Window", "window"]

_POLL_INTERVAL = 2.0

# How many consecutive polls may fail to remove the trigger file before
# the file trigger is given up on. A freshly created file can be held
# briefly by another process (an indexer or virus scanner, commonly on
# Windows), so one failure is retried on the next poll rather than
# treated as permanent.
_REMOVE_ATTEMPTS = 5


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
    need `every`; `duration` must be shorter than `every`. `on_signal`
    (a signal.Signals member, its number, or its name with or without
    the SIG prefix) and `on_file` (a path whose appearance opens a run,
    the file being removed as it is consumed, polled every couple of
    seconds) open runs from outside; with either and no other trigger
    the window opens only when kicked, and a kick with no `duration`
    closes any open run and starts the next, so a signal toggles.

    `report` is an output path template for the file destination,
    `on_report` a callable given each Report, and `retain` how many
    reports the window keeps on itself.

    start() arms the schedule and stop() takes it down, closing an open
    run; open() and close() drive runs by hand between them. Config
    windows are started by apply() and stopped by revert(); shutdown()
    (and so interpreter exit) closes any open run and delivers its
    reports.
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
        on_signal: str | int | signal.Signals | None = None,
        on_file: str | os.PathLike[str] | None = None,
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
        self._signal = _signal_named(on_signal) if on_signal is not None else None
        self._file = os.fspath(on_file) if on_file is not None else None

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
        # be a sink as well, registered while armed), possibly beneath
        # gating combinators, in which case the outer gate is what is
        # registered and the collector within is what is armed;
        # anything else must be a plain sink.

        self._collectors: list[tuple[Any, Collector]] = []
        self._sinks: list[Sink] = []

        for item in collect:
            core = _innermost(item)
            if isinstance(core, Collector):
                self._collectors.append((item, core))
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
        self._poll: Schedule | None = None
        self._remove_failures = 0
        self._kicks = 0
        self.errors = 0

    def __repr__(self) -> str:
        parts = [f"Window({self._name!r}"]
        for key in ("after", "duration", "every", "times", "at"):
            value = getattr(self, f"_{key}")
            if value is not None:
                parts.append(f", {key}={value!r}")
        if self._align:
            parts.append(", align=True")
        if self._signal is not None:
            parts.append(f", on_signal={self._signal.name!r}")
        if self._file is not None:
            parts.append(f", on_file={self._file!r}")
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
    def kicks(self) -> int:
        """How many times a signal or file trigger fired."""

        with self._lock:
            return self._kicks

    @property
    def reports(self) -> tuple[Report, ...]:
        """The most recent reports, oldest first, up to `retain`."""

        with self._lock:
            return tuple(self._reports)

    @property
    def collectors(self) -> tuple[Collector, ...]:
        return tuple(core for _, core in self._collectors)

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
        if self._signal is not None:
            clauses.append(f"on {self._signal.name}")
        if self._file is not None:
            clauses.append(f"on touch of {self._file}")
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
        elif self._kicked:
            clauses.append("until the next kick")
        elif not clauses:
            clauses.append("whole process")

        return ", ".join(clauses)

    @property
    def _kicked(self) -> bool:
        return self._signal is not None or self._file is not None

    @property
    def _scheduled(self) -> bool:
        # Whether anything opens runs on a timer, as opposed to only
        # when kicked from outside.

        return (
            self._after is not None
            or self._every is not None
            or self._at is not None
            or not self._kicked
        )

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Arm the schedule: open the first run now or at its trigger,
        install the signal handler or file poll, and repeat as
        configured. Idempotent. Installing a signal handler is only
        possible from the main thread; starting elsewhere raises."""

        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopped = False

        _register(self)

        if self._signal is not None:
            _listen(self._signal, self)

        if self._file is not None:
            self._poll = every(
                self._poll_file, _POLL_INTERVAL, name=f"window {self._name!r} poll"
            )

        if not self._scheduled:
            return

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

            if self._poll is not None:
                self._poll.cancel()
                self._poll = None

            planned = self._duration is not None or self._every is not None

        if self._signal is not None:
            _unlisten(self._signal, self)

        self.close(cut_short=planned)

    def kick(self) -> None:
        """What a signal or file trigger does: open a run, or with no
        duration set, close the open run and start the next. Refused
        and counted while a timed run is in progress."""

        with self._lock:
            self._kicks += 1
            if self._stopped:
                return

        if self._duration is None and self._run is not None:
            self.close()

        self.open()

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

            for outer, collector in self._collectors:
                if isinstance(outer, Sink):
                    _bind_context(collector, run.context)
                    add_sink(outer)
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

            for outer, collector in self._collectors:
                try:
                    collector.disarm()
                except Exception:
                    self._note_error(collector)
                if isinstance(outer, Sink):
                    remove_sink(outer)

            for sink in self._sinks:
                remove_sink(sink)
                _release(sink)

            reports: list[Report] = []
            for _, collector in self._collectors:
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

    def _poll_file(self) -> None:
        # The file trigger: when the file is there, consume it and open
        # a run. A file that cannot be removed would fire on every poll,
        # so removal is retried for a few polls (a new file can be held
        # briefly by a scanner or indexer) and then, still failing, is
        # warned about once and the poll stops.

        assert self._file is not None

        if not os.path.exists(self._file):
            return

        try:
            os.remove(self._file)
        except FileNotFoundError:
            # Consumed by another poll, or removed by hand, between the
            # existence check and the removal: nothing to open for.

            return
        except OSError as exc:
            self._remove_failures += 1

            if self._remove_failures < _REMOVE_ATTEMPTS:
                return

            warnings.warn(
                f"window {self._name!r} cannot remove its trigger file"
                f" {self._file!r} ({exc}) after {self._remove_failures}"
                f" attempts; the file trigger is disabled",
                RuntimeWarning,
                stacklevel=2,
            )
            with self._lock:
                if self._poll is not None:
                    self._poll.cancel()
                    self._poll = None
            return

        self._remove_failures = 0
        self.kick()

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


def _innermost(item: Any) -> Any:
    # The thing beneath any gating combinators wrapped around it.

    while isinstance(item, (Filter, Depth, Sample)):
        item = item._sink
    return item


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


def _signal_named(value: str | int | signal.Signals) -> signal.Signals:
    # Normalise a signal given as a signal.Signals member, its number,
    # or its name with or without the SIG prefix, to the member, so a
    # window reports it one way whichever form it was given in.

    if isinstance(value, bool):
        raise ValueError(f"on_signal must name a signal, got {value!r}")

    if isinstance(value, str):
        name = value.strip().upper()
        if not name.startswith("SIG"):
            name = "SIG" + name
        try:
            return signal.Signals[name]
        except KeyError:
            raise ValueError(
                f"on_signal {value!r} is not a signal on this platform"
            ) from None

    if isinstance(value, int):
        try:
            return signal.Signals(value)
        except ValueError:
            raise ValueError(
                f"on_signal {value!r} is not a signal number on this platform"
            ) from None

    raise ValueError(f"on_signal must name a signal, got {value!r}")


# One real handler per signal, dispatching to every window listening
# for it; the handler that was there before is chained after them when
# it was a Python callable, and restored when the last window leaves.

_listeners: dict[signal.Signals, tuple[Any, list[Window]]] = {}
_signal_lock = threading.Lock()


def _listen(signum: signal.Signals, window: Window) -> None:
    with _signal_lock:
        entry = _listeners.get(signum)
        if entry is not None:
            entry[1].append(window)
            return

        try:
            previous = signal.signal(signum, _dispatch)
        except ValueError as exc:
            raise RuntimeError(
                f"window {window.name!r}: a signal handler for"
                f" {signum.name} can only be installed from the main thread"
                f" ({exc})"
            ) from None

        _listeners[signum] = (previous, [window])


def _unlisten(signum: signal.Signals, window: Window) -> None:
    with _signal_lock:
        entry = _listeners.get(signum)
        if entry is None:
            return

        previous, windows = entry
        if window in windows:
            windows.remove(window)
        if windows:
            return

        del _listeners[signum]
        try:
            signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
        except (ValueError, TypeError, OSError):
            pass


def _dispatch(signum: int, frame: Any) -> None:
    # Runs on the main thread between bytecodes, so it does the least
    # possible: hands each listening window's kick to the scheduler
    # thread, where every other trigger opens runs, then chains to the
    # handler that was there before.

    with _signal_lock:
        entry = _listeners.get(signal.Signals(signum))
        previous, windows = entry if entry is not None else (None, [])
        windows = list(windows)

    for window in windows:
        once(window.kick, 0, name=f"window {window.name!r} signal")

    if callable(previous):
        previous(signum, frame)


# Every started window is registered so shutdown() (and so interpreter
# exit) can close an open run and deliver its reports.

_windows: weakref.WeakSet[Window] = weakref.WeakSet()
_registry_lock = threading.Lock()


def _register(window: Window) -> None:
    with _registry_lock:
        _windows.add(window)

    _on_shutdown("close window runs", _shutdown_windows, phase=WINDOWS)


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
