"""The sink layer: who is listening, and how events reach them.

Binding points emit events; sinks consume them. The recording gate is
"is anything listening", not "is there a timeline": a tape scoped to a
test is one kind of listener, and a streaming sink registered for the
life of the process is another. A binding whose events nobody wants
constructs no event at all, which is what keeps an applied but
unmonitored binding near-free.

Sinks live in a two-tier registry. Process sinks are a plain module
level list, visible to every thread, for sinks installed at startup and
rarely removed. Scoped sinks live in a context variable, so a
timeline's tape is visible only to the context that opened it,
propagating the way context does: into tasks, not into threads. Every
recorded event is delivered to both tiers, and the effective capture
level is the highest any active sink asks for.

Sequence numbers are allocated process-wide, not per sink: every sink
observing an event must agree on its identity, and events link to
their parents by sequence number, so the numbers must be unambiguous
across every sink active at once.
"""

from __future__ import annotations

import atexit
import contextvars
import json
import os
import queue
import random
import sys
import threading
import warnings
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from wrapt import MISSING

from .capture import (
    NONE,
    REFERENCE,
    CapturePolicy,
    _level_of,
    _resolve_policy,
    summarize,
    type_name,
)
from .events import Event, _own_time
from .exceptions import SinkErrorWarning


class Sink:
    """Base class for event consumers.

    A sink hears about each event at most twice: `on_enter` when the
    event is recorded, before the observed operation runs, then exactly
    one of `on_exit` or `on_error` when the operation completes or
    raises. The event's `kind` field says what was observed, so the
    protocol stays three methods however many kinds exist. An event
    that is never closed, such as a generator abandoned mid-iteration,
    gets an enter and no exit, which is itself information.

    The default implementations do nothing; a subclass overrides only
    the notifications it consumes. Notifications run on the thread that
    executed the observed operation, inside the recording path, so they
    should be quick, and code they call is never itself recorded.

    A sink must never take the observed application down: an exception
    raised from a notification is suppressed, counted on the sink's
    `errors` attribute, and reported with a SinkErrorWarning the first
    time only.

    `capture_args` and `capture_result` declare the capture levels this
    sink needs of the values on events it receives. The effective level
    for a recorded event is the highest any active sink declares, so
    one sink's modest needs never downgrade another's.
    """

    capture_args: CapturePolicy | str = REFERENCE
    capture_result: CapturePolicy | str = REFERENCE

    # How many notifications this sink has raised from, all suppressed.

    errors: int = 0

    def on_enter(self, event: Event) -> None:
        """Called when an event is recorded, before the operation runs."""

    def on_exit(self, event: Event) -> None:
        """Called when the operation behind the event completes."""

    def on_error(self, event: Event) -> None:
        """Called when the operation behind the event raises; the
        event's `exception` field holds what it raised."""

    def flush(self) -> None:
        """Push any buffered output out. Called for process sinks at
        interpreter shutdown; a sink that buffers overrides this."""


# The two tiers. Process sinks are deliberately a plain list rather
# than a contextvar, because a sink meant to observe the whole process
# must see events from every thread and task.

_process_sinks: list[Sink] = []
_scoped_sinks: contextvars.ContextVar[tuple[Sink, ...]] = contextvars.ContextVar(
    "wrapture_scoped_sinks", default=()
)

_registry_lock = threading.Lock()
_flush_registered = False


def add_sink(sink: Sink) -> Sink:
    """Register a process sink: one that hears every recorded event,
    from every thread and task, until removed.

    This is the tier for observing a whole running application; there
    is no enclosing scope, so pair it with remove_sink() when the sink
    should stop listening. For a scope that ends, use a timeline. The
    first registration installs an atexit handler that calls flush()
    on the process sinks still registered at interpreter shutdown, so
    the tail of a trace is not lost in a sink's buffers.

    Returns the sink, so registration can wrap construction.
    """

    global _flush_registered

    with _registry_lock:
        _process_sinks.append(sink)

        if not _flush_registered:
            _flush_registered = True
            atexit.register(_flush_process_sinks)

    return sink


def remove_sink(sink: Sink) -> None:
    """Remove a previously added process sink.

    Raises ValueError if the sink is not currently registered, so a
    mixed-up lifecycle fails loudly instead of leaving a sink listening
    forever.
    """

    with _registry_lock:
        try:
            _process_sinks.remove(sink)
        except ValueError:
            raise ValueError(f"{sink!r} is not a registered process sink") from None


def flush_sinks() -> None:
    """Flush every registered process sink now.

    The same operation the atexit handler performs at interpreter
    shutdown, callable directly for environments where atexit cannot be
    relied on: an embedded or sub interpreter may be destroyed without
    its atexit callbacks ever running. Hosts such as mod_wsgi provide
    their own process shutdown notification that fires while the
    interpreter is still fully alive; subscribe that notification to
    this function. Safe to call any number of times; a sink that raises
    from flush() is counted and warned about like any other sink error,
    and the remaining sinks still flush.
    """

    _flush_process_sinks()


def _flush_process_sinks() -> None:
    for sink in tuple(_process_sinks):
        try:
            sink.flush()
        except Exception:
            _note_sink_error(sink)


def _active_sinks() -> tuple[Sink, ...]:
    # The common cases allocate nothing: with no process sinks the
    # scoped tuple is returned as is, and the not-recording fast path
    # only asks whether the result is empty.

    scoped = _scoped_sinks.get()

    if not _process_sinks:
        return scoped
    return tuple(_process_sinks) + scoped


# The reentrancy guard. Set while the recording machinery itself runs,
# so an observed callable invoked from inside the recorder (a capture
# policy's repr, a sink's own bookkeeping) passes through instead of
# recording recursively without bound. Behaviour still applies on the
# guarded path: only recording is skipped.

_in_recorder: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "wrapture_in_recorder", default=False
)


_seq_lock = threading.Lock()
_seq = 0


def _next_seq() -> int:
    global _seq

    with _seq_lock:
        _seq += 1
        return _seq


def _note_sink_error(sink: Sink) -> None:
    # A broken sink must not take down the application: the error is
    # suppressed, counted on the sink, and warned about once, so a sink
    # failing on every event in a hot loop cannot flood the warnings.

    sink.errors = getattr(sink, "errors", 0) + 1

    if sink.errors == 1:
        warnings.warn(
            f"sink {sink!r} raised from a notification; the error was"
            f" suppressed and the observed call is unaffected. Further"
            f" failures of this sink are counted on its errors"
            f" attribute without warning again.",
            SinkErrorWarning,
            stacklevel=2,
        )


def _deliver(notification: str, event: Event, active: tuple[Sink, ...]) -> None:
    # One sink failing must not starve the others, so each call is
    # guarded individually.

    guard = _in_recorder.set(True)
    try:
        for sink in active:
            try:
                getattr(sink, notification)(event)
            except Exception:
                _note_sink_error(sink)
    finally:
        _in_recorder.reset(guard)


def _record_event(event: Event, active: tuple[Sink, ...]) -> Event:
    # Identity first, then delivery: a sink may read event.seq, and any
    # child recorded during the operation links to it.

    event.seq = _next_seq()
    _deliver("on_enter", event, active)
    return event


def _notify_exit(event: Event, active: tuple[Sink, ...]) -> None:
    # Completion goes to the sinks that saw the event enter, not to
    # whatever is active now: a coroutine or generator may complete in
    # a different context from the call that created it, and pairing
    # must not depend on that.

    _deliver("on_exit", event, active)


def _notify_error(event: Event, active: tuple[Sink, ...]) -> None:
    _deliver("on_error", event, active)


def _required_policy(active: tuple[Sink, ...], axis: str) -> CapturePolicy:
    # The effective capture policy for one axis ("capture_args" or
    # "capture_result"): the declaration with the highest level among
    # the active sinks, so a test's tape cannot downgrade what a
    # streaming sink sharing the process needs.

    required: CapturePolicy = NONE
    required_level = -1

    for sink in active:
        declared = _resolve_policy(getattr(sink, axis, REFERENCE))
        if declared is None:
            declared = REFERENCE

        level = _level_of(declared)
        if level > required_level:
            required, required_level = declared, level

    return required


class Printer(Sink):
    """A sink that prints events live, as they happen.

    One line when an operation begins, indented by nesting depth, and a
    closing line with the outcome when there is one to show: `->` for a
    result, `!!` for an exception, matching the markers tape.tree()
    uses. This is the live view of a trace, for watching calls while
    they run; tree() is the tidy reconstruction afterwards.

    Writes to the given stream, or to sys.stderr when none is given,
    resolved at write time so redirection is respected. Every line is
    flushed as written, so a trace is intact up to the moment of a
    crash or hang.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def _write(self, line: str) -> None:
        stream = self._stream if self._stream is not None else sys.stderr
        print(line, file=stream, flush=True)

    def on_enter(self, event: Event) -> None:
        """Print the operation as it begins."""

        self._write("  " * event.depth + str(event))

    def on_exit(self, event: Event) -> None:
        """Print the result, when one was captured."""

        if event.result is MISSING:
            return

        injected = " (injected)" if event.injected else ""
        where = event.label or event.path
        self._write("  " * event.depth + f"{where} -> {event.result!r}{injected}")

    def on_error(self, event: Event) -> None:
        """Print the exception the operation raised."""

        where = event.label or event.path
        exception = type(event.exception).__name__
        self._write("  " * event.depth + f"{where} !! {exception}")


def _forward(sink: Sink, notification: str, event: Event) -> None:
    # Combinators forward with the same isolation the registry gives
    # top-level sinks, so the count and the warning land on the sink
    # that actually broke, and its siblings still get the event.

    try:
        getattr(sink, notification)(event)
    except Exception:
        _note_sink_error(sink)


class Counter(Sink):
    """A sink that counts events and retains nothing.

    The count is of operations observed beginning, whether or not they
    completed. Declaring "none" on both capture axes matters: when no
    other active sink asks for more, recording skips value capture
    entirely, including signature binding, the dominant cost, so a
    counter over a hot method is cheap enough to leave on for a whole
    test suite.
    """

    capture_args: CapturePolicy | str = "none"
    capture_result: CapturePolicy | str = "none"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    @property
    def count(self) -> int:
        """How many operations this sink has heard begin."""

        with self._lock:
            return self._count

    def on_enter(self, event: Event) -> None:
        """Count the event."""

        with self._lock:
            self._count += 1

    def __repr__(self) -> str:
        return f"<Counter: {self.count}>"


@dataclass(frozen=True)
class PathStats:
    """Aggregated figures for one path: how many operations began, and
    the total, self, fastest and slowest execution times of those that
    closed with one.

    Execution time is the event's duration, except for generators,
    whose accumulated body time is used instead, since their wall
    duration includes the consumer's time between yields. self_total
    is total minus the time spent in observed children: the figure
    profilers rank by.
    """

    count: int
    total: float
    self_total: float
    min: float | None
    max: float | None


class Aggregate(Sink):
    """Per-path statistics in bounded memory.

    One row per path: how many operations began, and the total, self,
    fastest and slowest execution times of the ones that completed,
    exceptions included. Self time is computed as events close, from
    the parent links alone, so no events are retained; memory is
    bounded by the number of bound locations plus the operations in
    flight at any moment. Like Counter, it declares "none" capture on
    both axes, so it never causes values to be captured.
    """

    capture_args: CapturePolicy | str = "none"
    capture_result: CapturePolicy | str = "none"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, list[Any]] = {}

        # Children's execution time accumulated against each event
        # still in flight, keyed by seq; an event's self time is its
        # own time minus what its children deposited here.

        self._pending: dict[int, float] = {}

    @property
    def stats(self) -> dict[str, PathStats]:
        """A snapshot of the per-path figures, keyed by event path."""

        with self._lock:
            return {
                path: PathStats(row[0], row[1], row[4], row[2], row[3])
                for path, row in self._rows.items()
            }

    def on_enter(self, event: Event) -> None:
        """Count the operation against its path and mark it in flight."""

        with self._lock:
            row = self._rows.setdefault(event.path, [0, 0.0, None, None, 0.0])
            row[0] += 1

            self._pending[event.seq] = 0.0

    def _observe(self, event: Event) -> None:
        own = _own_time(event)

        with self._lock:
            children = self._pending.pop(event.seq, 0.0)

            if own is None:
                return

            # Deposit this event's time with its parent, if the parent
            # is still in flight; a parent that already closed (a late
            # child) can no longer be adjusted.

            if event.parent_id is not None and event.parent_id in self._pending:
                self._pending[event.parent_id] += own

            row = self._rows.setdefault(event.path, [0, 0.0, None, None, 0.0])
            row[1] += own
            row[2] = own if row[2] is None else min(row[2], own)
            row[3] = own if row[3] is None else max(row[3], own)
            row[4] += max(0.0, own - children)

    def on_exit(self, event: Event) -> None:
        """Fold the completed operation's time into its row."""

        self._observe(event)

    def on_error(self, event: Event) -> None:
        """Fold the failed operation's time into its row."""

        self._observe(event)


class Fanout(Sink):
    """One sink that delivers to several.

    Each notification is forwarded to every inner sink in order, with
    the same isolation the registry gives top-level sinks: a broken
    inner sink is counted and skipped, and the others still hear the
    event. The capture declarations are the highest any inner sink
    declares, read when the Fanout is constructed, so capture
    negotiation sees through the composition.
    """

    def __init__(self, *sinks: Sink) -> None:
        self._sinks = sinks
        self.capture_args = _required_policy(sinks, "capture_args")
        self.capture_result = _required_policy(sinks, "capture_result")

    def on_enter(self, event: Event) -> None:
        """Forward to every inner sink."""

        for sink in self._sinks:
            _forward(sink, "on_enter", event)

    def on_exit(self, event: Event) -> None:
        """Forward to every inner sink."""

        for sink in self._sinks:
            _forward(sink, "on_exit", event)

    def on_error(self, event: Event) -> None:
        """Forward to every inner sink."""

        for sink in self._sinks:
            _forward(sink, "on_error", event)

    def flush(self) -> None:
        """Flush every inner sink."""

        for sink in self._sinks:
            try:
                sink.flush()
            except Exception:
                _note_sink_error(sink)


class Filter(Sink):
    """Forward only the events a predicate accepts.

    The predicate is consulted once, when the event enters, and the
    decision sticks: an accepted event's exit or error is forwarded
    even if the fields the predicate looked at have changed since, so
    the inner sink always sees properly paired notifications. The
    capture declarations are the inner sink's, read at construction.
    """

    def __init__(self, predicate: Callable[[Event], bool], sink: Sink) -> None:
        self._predicate = predicate
        self._sink = sink
        self._lock = threading.Lock()
        self._accepted: weakref.WeakSet[Event] = weakref.WeakSet()

        self.capture_args = getattr(sink, "capture_args", REFERENCE)
        self.capture_result = getattr(sink, "capture_result", REFERENCE)

    def on_enter(self, event: Event) -> None:
        """Consult the predicate; forward and remember an accept."""

        if self._predicate(event):
            with self._lock:
                self._accepted.add(event)

            _forward(self._sink, "on_enter", event)

    def _close(self, notification: str, event: Event) -> None:
        with self._lock:
            accepted = event in self._accepted
            self._accepted.discard(event)

        if accepted:
            _forward(self._sink, notification, event)

    def on_exit(self, event: Event) -> None:
        """Forward the close of an accepted event."""

        self._close("on_exit", event)

    def on_error(self, event: Event) -> None:
        """Forward the failure of an accepted event."""

        self._close("on_error", event)

    def flush(self) -> None:
        """Flush the inner sink."""

        self._sink.flush()


class Depth(Sink):
    """Forward only the top levels of the call tree.

    An event at depth below `max_depth` passes; deeper ones are
    dropped, so Depth(1, ...) is roots only and Depth(2, ...) is roots
    and their direct children. Depth is fixed when an event is
    recorded, so pairing is consistent without any bookkeeping. The
    capture declarations are the inner sink's, read at construction.
    """

    def __init__(self, max_depth: int, sink: Sink) -> None:
        if max_depth < 1:
            raise ValueError(
                f"max_depth must be a positive number of levels, got {max_depth!r}"
            )

        self._max_depth = max_depth
        self._sink = sink

        self.capture_args = getattr(sink, "capture_args", REFERENCE)
        self.capture_result = getattr(sink, "capture_result", REFERENCE)

    def on_enter(self, event: Event) -> None:
        """Forward events above the depth cut."""

        if event.depth < self._max_depth:
            _forward(self._sink, "on_enter", event)

    def on_exit(self, event: Event) -> None:
        """Forward the close of events above the depth cut."""

        if event.depth < self._max_depth:
            _forward(self._sink, "on_exit", event)

    def on_error(self, event: Event) -> None:
        """Forward the failure of events above the depth cut."""

        if event.depth < self._max_depth:
            _forward(self._sink, "on_error", event)

    def flush(self) -> None:
        """Flush the inner sink."""

        self._sink.flush()


class Sample(Sink):
    """Forward a random fraction of call trees.

    The keep-or-drop decision is made once per tree, when its root
    enters, and inherited by everything beneath: sampling per event
    would emit children whose parents were never seen, orphaning them
    in the output. `rate` is the probability a tree is kept, from 0.0
    (nothing) to 1.0 (everything). The capture declarations are the
    inner sink's, read at construction.
    """

    def __init__(self, rate: float, sink: Sink) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"rate must be between 0.0 and 1.0, got {rate!r}")

        self._rate = rate
        self._sink = sink
        self._random = random.random
        self._lock = threading.Lock()
        self._kept: set[int] = set()

        self.capture_args = getattr(sink, "capture_args", REFERENCE)
        self.capture_result = getattr(sink, "capture_result", REFERENCE)

    def on_enter(self, event: Event) -> None:
        """Decide at the root; inherit the decision below it."""

        with self._lock:
            if event.parent_id is None:
                keep = self._random() < self._rate
            else:
                keep = event.parent_id in self._kept

            if keep:
                self._kept.add(event.seq)

        if keep:
            _forward(self._sink, "on_enter", event)

    def _close(self, notification: str, event: Event) -> None:
        # Children close before their parent does, so the parent's
        # entry is still present for them and can be dropped at the
        # parent's own close.

        with self._lock:
            kept = event.seq in self._kept
            self._kept.discard(event.seq)

        if kept:
            _forward(self._sink, notification, event)

    def on_exit(self, event: Event) -> None:
        """Forward the close of a sampled tree's event."""

        self._close("on_exit", event)

    def on_error(self, event: Event) -> None:
        """Forward the failure of a sampled tree's event."""

        self._close("on_error", event)

    def flush(self) -> None:
        """Flush the inner sink."""

        self._sink.flush()


def _jsonable(value: Any, depth: int = 8) -> Any:
    # Reduce a captured value to something json.dumps accepts, whatever
    # the capture level stored: summary-captured values are already
    # strings and scalars, but reference and snapshot captures can hold
    # arbitrary objects. The depth bound stops runaway recursion on
    # deep or self-referential containers; past it only the type is
    # reported, which calls no user code.

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if depth <= 0:
        return type_name(value)

    if isinstance(value, (list, tuple)):
        return [_jsonable(item, depth - 1) for item in value]

    if isinstance(value, dict):
        return {
            (key if isinstance(key, str) else str(key)): _jsonable(item, depth - 1)
            for key, item in value.items()
        }

    summarized = summarize(value)
    if summarized is None or isinstance(summarized, (bool, int, float, str)):
        return summarized
    return str(summarized)


def _event_record(event: Event) -> dict[str, Any]:
    # The stable serialised form of one event, shared by JSONLines and
    # the exporters. Identity, position and thread are always present;
    # parent_id is null for a root. Everything else appears only when
    # observed, so an absent "result" (nothing captured) stays
    # distinguishable from "result": null (the call returned None).

    record: dict[str, Any] = {
        "seq": event.seq,
        "parent_id": event.parent_id,
        "depth": event.depth,
        "kind": event.kind,
        "path": event.path,
        "thread_id": event.thread_id,
        "thread_name": event.thread_name,
    }

    if event.label is not None:
        record["label"] = event.label

    if event.started is not None:
        record["started"] = event.started
    if event.duration is not None:
        record["duration"] = event.duration
    if event.body_duration is not None:
        record["body_duration"] = event.body_duration
    if event.items is not None:
        record["items"] = event.items

    if event.arguments is not None:
        record["arguments"] = _jsonable(event.arguments)
    if event.args is not None:
        record["args"] = _jsonable(list(event.args))
    if event.kwargs is not None:
        record["kwargs"] = _jsonable(event.kwargs)
    if event.forwarded is not None:
        forwarded_args, forwarded_kwargs = event.forwarded
        record["forwarded"] = {
            "args": _jsonable(list(forwarded_args)),
            "kwargs": _jsonable(forwarded_kwargs),
        }

    if event.result is not MISSING:
        record["result"] = _jsonable(event.result)
    if event.exception is not None:
        record["exception"] = {
            "type": type(event.exception).__name__,
            "message": str(event.exception),
        }

    if event.value is not MISSING:
        record["value"] = _jsonable(event.value)
    if event.previous is not MISSING:
        record["previous"] = _jsonable(event.previous)

    if event.injected:
        record["injected"] = True
    if event.stack is not None:
        record["stack"] = event.stack
    if event.data:
        record["data"] = _jsonable(event.data)

    return record


_STOP = object()


class JSONLines(Sink):
    """Stream each completed event to a file, one JSON object per line.

    A line is written when an event closes, exit and error alike, so
    every line carries the outcome and timing; an event that never
    closes is never written. Lines therefore appear in completion
    order, children before the operation that contains them; sort by
    "seq" and rebuild nesting from "parent_id" to recover the tree.
    Fields present on every line: seq, parent_id (null for a root),
    depth, kind, path, thread_id and thread_name. Everything else
    appears only when observed, so an absent "result" (nothing
    captured) stays distinguishable from "result": null (the
    operation returned None).

    The observed application is never blocked on I/O: lines go onto a
    bounded queue drained by a background writer thread, and when the
    queue is full the line is dropped and counted on `dropped` rather
    than making the application wait. flush() blocks briefly until
    queued lines are on disk; close() flushes, stops the writer, and
    closes the file, and anything recorded after that is dropped and
    counted.

    Declares "summary" capture on both axes: a streaming sink must
    neither retain live objects nor fail on unserialisable ones.
    Values that reach it captured by reference regardless (a binding
    override, another sink's requirement) are reduced to summaries at
    serialisation time.
    """

    capture_args: CapturePolicy | str = "summary"
    capture_result: CapturePolicy | str = "summary"

    def __init__(self, path: str | os.PathLike[str], *, limit: int = 1000) -> None:
        self._path = os.fspath(path)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=limit)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """Lines dropped because the queue was full or the sink was
        closed, counted so backpressure is a visible fact."""

        with self._lock:
            return self._dropped

    def _submit(self, event: Event) -> None:
        # Serialise inline, cheaply, on the observed thread; only the
        # write is handed to the background thread. The writer starts
        # lazily with the first line, so a constructed but unused sink
        # owns no thread and no open file.

        with self._lock:
            if self._closed:
                self._dropped += 1
                return

            if not self._started:
                self._started = True
                self._thread = threading.Thread(
                    target=self._run, name="wrapture-jsonlines", daemon=True
                )
                self._thread.start()

        line = json.dumps(
            _event_record(event), ensure_ascii=False, separators=(",", ":")
        )

        try:
            self._queue.put_nowait(line + "\n")
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def on_exit(self, event: Event) -> None:
        """Write the completed event as one line."""

        self._submit(event)

    def on_error(self, event: Event) -> None:
        """Write the failed event as one line."""

        self._submit(event)

    def _run(self) -> None:
        # The writer owns the file for its whole life. If it cannot be
        # opened the sink is broken: the error is counted, and the
        # queue is drained and discarded so flush() barriers never
        # hang and the application never blocks.

        try:
            stream = open(self._path, "a", encoding="utf-8")
        except Exception:
            _note_sink_error(self)

            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                if isinstance(item, threading.Event):
                    item.set()

        try:
            while True:
                item = self._queue.get()

                if item is _STOP:
                    break

                if isinstance(item, threading.Event):
                    try:
                        stream.flush()
                    except Exception:
                        _note_sink_error(self)
                    item.set()
                    continue

                try:
                    stream.write(item)

                    # Batch under load, current when idle: flush only
                    # once the queue has gone quiet.

                    if self._queue.empty():
                        stream.flush()
                except Exception:
                    _note_sink_error(self)
        finally:
            try:
                stream.flush()
                stream.close()
            except Exception:
                _note_sink_error(self)

    def flush(self) -> None:
        """Block briefly until every queued line is on disk."""

        with self._lock:
            if not self._started or self._closed:
                return

        barrier = threading.Event()

        try:
            self._queue.put(barrier, timeout=5)
        except queue.Full:
            return

        barrier.wait(timeout=5)

    def close(self) -> None:
        """Write out the queue, stop the writer, and close the file.

        Anything recorded after closing is dropped and counted on
        `dropped`. Idempotent.
        """

        with self._lock:
            if self._closed:
                return

            self._closed = True
            started = self._started
            thread = self._thread

        if not started or thread is None:
            return

        # The stop marker queues behind every pending line, so the
        # writer drains the lot before it exits and closes the file.

        try:
            self._queue.put(_STOP, timeout=5)
        except queue.Full:
            return

        thread.join(timeout=5)
