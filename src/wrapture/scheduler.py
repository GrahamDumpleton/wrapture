"""The process-level timer that drives time-based behaviour.

One daemon thread per process, started lazily by the first thing that
needs it (a JSONLines or Printer rotate=, later a window schedule),
firing callbacks at intervals. Two kinds of interval:

- Relative: "every N seconds" measured on time.monotonic() from the
  moment the schedule was made, so a system clock step cannot make a
  run fire early, late or twice.
- Aligned: on the wall-clock boundary in local time, so hourly means
  on the hour and daily means at local midnight. Each next occurrence
  is computed afresh from the local clock after the previous one, so
  daylight saving transitions behave as a wall clock does rather than
  drifting by an hour.

Callbacks run on the scheduler thread and must be quick; an exception
from one is warned about and the schedule continues.
"""

from __future__ import annotations

import heapq
import itertools
import re
import threading
import time
import warnings
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from typing import Any

__all__ = ["Schedule", "every", "once", "parse_duration"]


_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])")


def parse_duration(value: str | int | float) -> float:
    """Seconds from a duration: a number is taken as seconds already; a
    string is one or more number-unit pairs, such as "30s", "15m",
    "1h", "2d" or "1h30m"."""

    if isinstance(value, bool):
        raise ValueError(f"duration must be a number or a string, not {value!r}")

    if isinstance(value, (int, float)):
        if value <= 0:
            raise ValueError(f"duration must be positive, not {value!r}")
        return float(value)

    if not isinstance(value, str):
        raise ValueError(f"duration must be a number or a string, not {value!r}")

    text = value.strip().lower()
    position = 0
    total = 0.0

    while position < len(text):
        match = _DURATION.match(text, position)
        if match is None:
            raise ValueError(
                f"duration {value!r} is not a number with a unit of s, m, h or"
                f" d, such as '30s', '15m', '1h' or '1h30m'"
            )

        total += float(match.group(1)) * _UNITS[match.group(2)]
        position = match.end()

    if total <= 0:
        raise ValueError(f"duration must be positive, not {value!r}")

    return total


def _next_aligned(interval: float, now: float | None = None) -> float:
    # The next wall-clock boundary at or after now, in epoch seconds:
    # the grid starts at local midnight, so an hourly interval fires on
    # the hour and a daily one at midnight, computed from the local
    # clock each time so it follows daylight saving.

    moment = time.time() if now is None else now
    local = time.localtime(moment)
    midnight = time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
    )

    elapsed = moment - midnight
    slots = int(elapsed // interval) + 1

    return midnight + slots * interval


def _next_at(
    hhmm: str,
    now: float | None = None,
    *,
    skip_day: tuple[int, int, int] | None = None,
    tz: tzinfo | None = None,
) -> float:
    # The next occurrence of a wall-clock time "HH:MM" after now, in
    # epoch seconds, in local time (or `tz`, an injection point for
    # tests): today if still ahead, otherwise a following day. A
    # candidate is only accepted when the clock actually reads that
    # time, so on the night the clocks go forward a time that does not
    # exist is skipped to the next day rather than shifted; and
    # `skip_day` names a date already fired on, so on the night they go
    # back the repeated hour does not fire twice.

    hours, minutes = _parse_hhmm(hhmm)
    moment = time.time() if now is None else now
    today = datetime.fromtimestamp(moment, tz).date()

    for days in range(0, 4):
        day = today + timedelta(days=days)
        candidate = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)
        stamp = candidate.timestamp()

        if stamp <= moment:
            continue

        reads = datetime.fromtimestamp(stamp, tz)
        if (reads.hour, reads.minute) != (hours, minutes):
            continue
        if skip_day is not None and (reads.year, reads.month, reads.day) == skip_day:
            continue

        return stamp

    raise ValueError(f"no occurrence of {hhmm!r} found in the coming days")


def _parse_hhmm(value: str) -> tuple[int, int]:
    match = (
        re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
        if isinstance(value, str)
        else None
    )
    if match is None:
        raise ValueError(f"time of day {value!r} is not HH:MM, such as '22:00'")

    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise ValueError(f"time of day {value!r} is out of range")

    return hours, minutes


class Schedule:
    """A handle on a callback registered with every() or once(); cancel()
    stops it, and `fired` counts how many times it has run."""

    def __init__(
        self,
        callback: Callable[[], Any],
        interval: float,
        align: bool,
        name: str,
        *,
        repeat: bool = True,
    ) -> None:
        self._callback = callback
        self._interval = interval
        self._align = align
        self._name = name
        self._repeat = repeat
        self._cancelled = False
        self.fired = 0

    def __repr__(self) -> str:
        if not self._repeat:
            return f"Schedule({self._name!r}, once after {self._interval})"
        return f"Schedule({self._name!r}, every={self._interval}, align={self._align})"

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Stop the schedule; a callback already running completes."""

        self._cancelled = True

    def _delay(self) -> float:
        # Seconds until the next firing: from the wall clock for an
        # aligned schedule, a plain interval otherwise.

        if not self._align:
            return self._interval

        # A firing that lands a hair before its boundary (the monotonic
        # wait and the wall clock do not agree to the microsecond) must
        # not fire again for the same boundary, so anything closer than
        # a second is taken as that boundary and the next one is used.

        delay = _next_aligned(self._interval) - time.time()
        if delay < 1.0:
            delay += self._interval
        return delay

    def _fire(self) -> None:
        try:
            self._callback()
        except Exception as exc:
            warnings.warn(
                f"scheduled callback {self._name!r} raised {exc!r}; the"
                f" schedule continues",
                RuntimeWarning,
                stacklevel=2,
            )
        finally:
            self.fired += 1


class _Scheduler:
    # The single timer thread. A heap of (due, sequence, schedule) on
    # the monotonic clock; the thread waits until the earliest is due,
    # fires it, and re-queues it unless cancelled. Registering wakes the
    # thread so a shorter interval added later is honoured.

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._heap: list[tuple[float, int, Schedule]] = []
        self._counter = itertools.count()
        self._thread: threading.Thread | None = None

    def add(self, schedule: Schedule) -> None:
        with self._condition:
            due = time.monotonic() + schedule._delay()
            heapq.heappush(self._heap, (due, next(self._counter), schedule))

            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="wrapture-scheduler", daemon=True
                )
                self._thread.start()

            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while True:
                    if not self._heap:
                        self._condition.wait()
                        continue

                    due, _, schedule = self._heap[0]

                    if schedule.cancelled:
                        heapq.heappop(self._heap)
                        continue

                    remaining = due - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue

                    heapq.heappop(self._heap)
                    break

            schedule._fire()

            with self._condition:
                if schedule._repeat and not schedule.cancelled:
                    due = time.monotonic() + schedule._delay()
                    heapq.heappush(self._heap, (due, next(self._counter), schedule))


_scheduler = _Scheduler()


def every(
    callback: Callable[[], Any],
    interval: str | int | float,
    *,
    align: bool = False,
    name: str = "callback",
) -> Schedule:
    """Run `callback` repeatedly at `interval` (a duration string such
    as "1h", or seconds), on the wall-clock boundary in local time when
    `align` is true, and return the Schedule handle to cancel it."""

    schedule = Schedule(callback, parse_duration(interval), align, name)
    _scheduler.add(schedule)
    return schedule


def once(
    callback: Callable[[], Any], delay: float, *, name: str = "callback"
) -> Schedule:
    """Run `callback` once, `delay` seconds from now on the monotonic
    clock, and return the Schedule handle to cancel it."""

    schedule = Schedule(callback, max(0.0, float(delay)), False, name, repeat=False)
    _scheduler.add(schedule)
    return schedule
