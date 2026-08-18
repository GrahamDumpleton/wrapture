"""The built-in collectors: numbers kept in bounded memory while a
window's run is open, rendered as a Report when it closes.

Counter counts operations; Aggregate keeps one row of timing figures
per path. Both are collectors (arm, disarm, report, reset) and sinks
at once: a window registers them as process sinks while armed, and in
code they can equally be registered with add_sink() for the life of a
test suite and read directly (`count`, `stats`). Both declare "none"
capture on both axes, so they never cause values to be captured, and
a counter over a hot method costs a fraction of what recording does.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .capture import CapturePolicy
from .events import Event, _own_time
from .sinks import Sink
from .windows import Report, Run

__all__ = ["Aggregate", "Counter", "PathStats"]


def _span(seconds: float) -> str:
    # A duration for a report header: 30.0s, 2m 5s, 1h 0m 0s.

    if seconds < 60:
        return f"{seconds:.1f}s"

    whole = int(round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def _header(kind: str, name: str, run: Run) -> str:
    # The line every report starts with: what, which run, when (local,
    # with offset), how long, and whose process.

    assert run.ended is not None
    started: datetime = run.started
    ended: datetime = run.ended

    offset = ended.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else ""

    line = (
        f'{kind} "{name}" run {run.number},'
        f" {started:%Y-%m-%d %H:%M:%S} to {ended:%H:%M:%S} {offset}"
        f" ({_span(run.duration or 0.0)}), pid {os.getpid()}"
    )
    if run.cut_short:
        line += " (cut short)"
    return line


def _figure(seconds: float | None) -> str:
    # A timing cell: adaptive units, blank when there is no figure.

    if seconds is None:
        return ""
    if seconds >= 1.0:
        return f"{seconds:.3f}s"
    if seconds >= 0.001:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds * 1_000_000:.0f}us"


class Counter(Sink):
    """Count operations, retaining nothing.

    The count is of operations observed beginning, whether or not they
    completed. As a collector its report is that one number; as a sink
    left registered for a whole test suite, `count` is read directly.
    """

    capture_args: CapturePolicy | str = "none"
    capture_result: CapturePolicy | str = "none"

    kind = "counter"

    def __init__(self, name: str = "counter") -> None:
        self.name = name
        self._lock = threading.Lock()
        self._count = 0

    @property
    def count(self) -> int:
        """How many operations this collector has heard begin."""

        with self._lock:
            return self._count

    def on_enter(self, event: Event) -> None:
        """Count the event."""

        with self._lock:
            self._count += 1

    def __repr__(self) -> str:
        return f"<Counter {self.name!r}: {self.count}>"

    def arm(self) -> None:
        """Nothing to start: counting happens while registered."""

    def disarm(self) -> None:
        """Nothing to stop."""

    def reset(self) -> None:
        """Clear the count for the next run."""

        with self._lock:
            self._count = 0

    def report(self, run: Run) -> Report:
        """The run's count as a Report; `data` is {"count": n}."""

        count = self.count
        plural = "" if count == 1 else "s"
        text = f"{_header(self.kind, self.name, run)}\n{count:,} operation{plural}\n"

        assert run.ended is not None
        return Report(
            kind=self.kind,
            name=self.name,
            window=run.window,
            run=run.number,
            started=run.started,
            ended=run.ended,
            duration=run.duration or 0.0,
            text=text,
            data={"count": count},
            cut_short=run.cut_short,
        )


@dataclass(frozen=True)
class PathStats:
    """Aggregated figures for one path: how many operations began, how
    many completed and how many of those raised, and the total, self,
    fastest and slowest execution times of the ones that completed.

    Execution time is the event's duration, except for generators,
    whose accumulated body time is used instead, since their wall
    duration includes the consumer's time between yields. self_total
    is total minus the time spent in observed children: the figure
    profilers rank by.
    """

    count: int
    completed: int
    errors: int
    total: float
    self_total: float
    min: float | None
    max: float | None

    @property
    def per_call(self) -> float | None:
        """Mean execution time per completed operation."""

        if not self.completed:
            return None
        return self.total / self.completed


class Aggregate(Sink):
    """Per-path statistics in bounded memory.

    One row per path: how many operations began, how many completed
    (and how many of those raised), and the total, self, fastest and
    slowest execution times of the completed ones. Self time is
    computed as events close, from the parent links alone, so no
    events are retained; memory is bounded by the number of bound
    locations plus the operations in flight at any moment. As a
    collector its report is the table sorted by self time; as a sink
    left registered, `stats` is read directly.
    """

    capture_args: CapturePolicy | str = "none"
    capture_result: CapturePolicy | str = "none"

    kind = "aggregate"

    def __init__(self, name: str = "aggregate") -> None:
        self.name = name
        self._lock = threading.Lock()

        # Row layout: count, completed, errors, total, min, max, self.

        self._rows: dict[str, list[Any]] = {}

        # Children's execution time accumulated against each event
        # still in flight, keyed by seq; an event's self time is its
        # own time minus what its children deposited here.

        self._pending: dict[int, float] = {}

    def __repr__(self) -> str:
        return f"<Aggregate {self.name!r}: {len(self._rows)} paths>"

    @property
    def stats(self) -> dict[str, PathStats]:
        """A snapshot of the per-path figures, keyed by event path."""

        with self._lock:
            return {
                path: PathStats(row[0], row[1], row[2], row[3], row[6], row[4], row[5])
                for path, row in self._rows.items()
            }

    def _row(self, path: str) -> list[Any]:
        return self._rows.setdefault(path, [0, 0, 0, 0.0, None, None, 0.0])

    def on_enter(self, event: Event) -> None:
        """Count the operation against its path and mark it in flight."""

        with self._lock:
            self._row(event.path)[0] += 1
            self._pending[event.seq] = 0.0

    def _observe(self, event: Event, *, failed: bool) -> None:
        own = _own_time(event)

        with self._lock:
            children = self._pending.pop(event.seq, 0.0)

            row = self._row(event.path)
            row[1] += 1
            if failed:
                row[2] += 1

            if own is None:
                return

            # Deposit this event's time with its parent, if the parent
            # is still in flight; a parent that already closed (a late
            # child) can no longer be adjusted.

            if event.parent_id is not None and event.parent_id in self._pending:
                self._pending[event.parent_id] += own

            row[3] += own
            row[4] = own if row[4] is None else min(row[4], own)
            row[5] = own if row[5] is None else max(row[5], own)
            row[6] += max(0.0, own - children)

    def on_exit(self, event: Event) -> None:
        """Fold the completed operation's time into its row."""

        self._observe(event, failed=False)

    def on_error(self, event: Event) -> None:
        """Fold the failed operation's time into its row."""

        self._observe(event, failed=True)

    def arm(self) -> None:
        """Nothing to start: figures accumulate while registered."""

    def disarm(self) -> None:
        """Nothing to stop."""

    def reset(self) -> None:
        """Clear every row for the next run; operations still in flight
        are forgotten too, so a run's figures are its own."""

        with self._lock:
            self._rows.clear()
            self._pending.clear()

    def report(self, run: Run) -> Report:
        """The run's figures as a Report.

        `text` is a table sorted by self time, one row per path with
        calls, total, self, per-call, min and max, and an errors column
        when any operation raised; a path begun but never completed
        shows its count with the timing cells blank. `data` is
        {"paths": {path: {"count", "completed", "errors", "total",
        "self", "min", "max"}}, "begun": n, "completed": n, "raised": n},
        with the same ordering as the table.
        """

        stats = self.stats
        ordered = sorted(
            stats.items(), key=lambda item: item[1].self_total, reverse=True
        )

        begun = sum(row.count for row in stats.values())
        completed = sum(row.completed for row in stats.values())
        raised = sum(row.errors for row in stats.values())
        any_errors = raised > 0

        lines = [_header(self.kind, self.name, run)]
        lines.append(
            f"{len(stats):,} path{'s' if len(stats) != 1 else ''},"
            f" {begun:,} operations begun, {completed:,} completed,"
            f" {raised:,} raised"
        )
        lines.append("")

        columns = ["calls", "total", "self", "per-call", "min", "max"]
        if any_errors:
            columns.append("errors")

        rows: list[list[str]] = []
        for _, row in ordered:
            cells = [
                f"{row.count:,}",
                _figure(row.total) if row.completed else "",
                _figure(row.self_total) if row.completed else "",
                _figure(row.per_call),
                _figure(row.min),
                _figure(row.max),
            ]
            if any_errors:
                cells.append(f"{row.errors:,}" if row.errors else "")
            rows.append(cells)

        widths = [
            max(len(title), *(len(cells[index]) for cells in rows), 0)
            for index, title in enumerate(columns)
        ]

        lines.append(
            "  ".join(
                title.rjust(width) for title, width in zip(columns, widths, strict=True)
            )
            + "  path"
        )
        for (path, _), cells in zip(ordered, rows, strict=True):
            lines.append(
                "  ".join(
                    cell.rjust(width) for cell, width in zip(cells, widths, strict=True)
                )
                + f"  {path}"
            )

        data = {
            "paths": {
                path: {
                    "count": row.count,
                    "completed": row.completed,
                    "errors": row.errors,
                    "total": row.total,
                    "self": row.self_total,
                    "min": row.min,
                    "max": row.max,
                }
                for path, row in ordered
            },
            "begun": begun,
            "completed": completed,
            "raised": raised,
        }

        assert run.ended is not None
        return Report(
            kind=self.kind,
            name=self.name,
            window=run.window,
            run=run.number,
            started=run.started,
            ended=run.ended,
            duration=run.duration or 0.0,
            text="\n".join(lines) + "\n",
            data=data,
            cut_short=run.cut_short,
        )
