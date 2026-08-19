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

import contextvars
import json
import os
import queue
import random
import sys
import threading
import time
import warnings
import weakref
from collections.abc import Callable
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
from .events import Event, _format_time
from .exceptions import ConfigWarning, SinkErrorWarning
from .lifecycle import SINKS, _on_shutdown
from .outputs import OutputPath, open_output
from .scheduler import Schedule, every, parse_duration


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


def add_sink(sink: Sink) -> Sink:
    """Register a process sink: one that hears every recorded event,
    from every thread and task, until removed.

    This is the tier for observing a whole running application; there
    is no enclosing scope, so pair it with remove_sink() when the sink
    should stop listening. For a scope that ends, use a timeline. The
    first registration adds flushing the process sinks to what
    shutdown() does at interpreter exit, so the tail of a trace is not
    lost in a sink's buffers.

    Returns the sink, so registration can wrap construction.
    """

    with _registry_lock:
        _process_sinks.append(sink)

    _on_shutdown("flush process sinks", _flush_process_sinks, phase=SINKS)

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

    The sink half of what shutdown() does at interpreter exit, for
    when a trace should be on disk mid-run. Safe to call any number of
    times; a sink that raises from flush() is counted and warned about
    like any other sink error, and the remaining sinks still flush.
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


def _note_sink_error(sink: Any) -> None:
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


def _rotation(
    sink: Sink,
    path: OutputPath,
    rotate: str | int | float | None,
    align: bool,
) -> tuple[float | None, bool]:
    # Validate a sink's rotate=/align= against its path template at
    # construction: align without rotate is meaningless, and rotating
    # a path with no time variable reopens the same file, which is
    # legal but pointless and almost certainly a mistake in the path.

    if rotate is None:
        if align:
            raise ValueError("align=True needs a rotate= interval")
        return None, False

    interval = parse_duration(rotate)

    if not path.timed:
        warnings.warn(
            f"{type(sink).__name__} path {path.template!r} has no time"
            f" variable, so rotate= reopens the same file each time; add"
            f" {{date}}, {{time}} or {{now:...}} to the path to rotate to"
            f" a new file",
            ConfigWarning,
            stacklevel=3,
        )

    return interval, align


class Printer(Sink):
    """A sink that prints events live, as they happen.

    One line when an operation begins, indented by nesting depth, and a
    closing line with the outcome and how long it took: `->` for a
    result, `!!` for an exception, matching the markers tape.tree()
    uses. This is the live view of a trace, for watching calls while
    they run; tree() is the tidy reconstruction afterwards.

    Writes to `stream` when one is given (any text-writable object: an
    open file, sys.stdout, an io.StringIO), or appends to the file at
    `path`, opened on the first line written, or to sys.stderr when
    neither is given, resolved at write time so redirection is
    respected. Every line is flushed as written, so a trace is intact
    up to the moment of a crash or hang.

    `path` is an output path template ({date}, {time}, {pid}, {name}
    and the rest; see wrapture.outputs), and `rotate`/`align` reopen
    it on an interval exactly as for JSONLines; `name` is what {name}
    expands to.

    `timing` (on by default) appends the elapsed time to each closing
    line, in the units tape.tree() uses, and for a streamed body (a
    generator, a request) the time spent in the body and how many items
    or chunks it produced. Switch it off where stable output matters
    more, such as a doctest or a diff. `timestamps` prefixes each
    opening line with the local wall-clock time, HH:MM:SS.mmm; off by
    default because at a terminal it is noise, on is what you want when
    the output goes to a file.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        path: str | os.PathLike[str] | None = None,
        name: str = "printer",
        rotate: str | int | float | None = None,
        align: bool = False,
        timing: bool = True,
        timestamps: bool = False,
    ) -> None:
        if stream is not None and path is not None:
            raise ValueError("Printer takes either stream or path, not both")
        if path is None and (rotate is not None or align):
            raise ValueError("Printer rotate= and align= need a path")

        self._stream = stream
        self._path = OutputPath(path, name=name) if path is not None else None
        self._timing = timing
        self._timestamps = timestamps

        self._lock = threading.Lock()
        self._file: TextIO | None = None
        self._current: str | None = None
        self._closed = False

        self._schedule: Schedule | None = None
        if self._path is not None:
            self._interval, self._align = _rotation(self, self._path, rotate, align)

    def __repr__(self) -> str:
        if self._path is None:
            return "Printer()"
        if self._current is None:
            return f"Printer(path={self._path.template!r})"
        return f"Printer(path={self._path.template!r}, writing={self._current!r})"

    @property
    def path(self) -> str | None:
        """The file currently being written, or None when the printer
        writes to a stream or has not opened its file yet."""

        with self._lock:
            return self._current

    def _write(self, line: str) -> None:
        with self._lock:
            if self._path is not None:
                if self._closed:
                    return
                if self._file is None:
                    self._open()
                stream = self._file
            elif self._stream is not None:
                stream = self._stream
            else:
                stream = sys.stderr

            print(line, file=stream, flush=True)

    def _open(self) -> None:
        # Under the lock. Expand the template afresh and open what it
        # names; the rotation schedule starts with the first open so an
        # unused printer owns no timer.

        assert self._path is not None

        self._current = self._path.expand()
        self._file = open_output(self._current, "a")

        if self._interval is not None and self._schedule is None:
            self._schedule = every(
                self.reopen, self._interval, align=self._align, name=repr(self)
            )

    def _prefix(self, event: Event) -> str:
        indent = "  " * event.depth
        if not self._timestamps:
            return indent

        # Closing lines are not timestamped: the opening time plus the
        # duration locates them, and it keeps the tree readable, so
        # they are padded to keep the indentation aligned.

        return f"{_wall_clock()} {indent}"

    def _suffix(self, event: Event) -> str:
        if not self._timing or event.duration is None:
            return ""

        # A streamed body reports its split too: the accumulated time
        # spent producing items (or body chunks, for a request) and how
        # many there were, when there were any.

        elapsed = _format_time(event.duration)
        if event.body_duration is not None and event.items:
            unit = "chunk" if event.kind == "request" else "item"
            plural = "s" if event.items != 1 else ""
            body = _format_time(event.body_duration)
            return f" [{elapsed}, body {body} over {event.items} {unit}{plural}]"

        return f" [{elapsed}]"

    def on_enter(self, event: Event) -> None:
        """Print the operation as it begins."""

        self._write(self._prefix(event) + str(event))

    def on_exit(self, event: Event) -> None:
        """Print the outcome: the result when one was captured, and the
        elapsed time when timing is on."""

        suffix = self._suffix(event)
        if event.result is MISSING and not suffix:
            return

        indent = "  " * event.depth
        pad = " " * len(_wall_clock()) + " " if self._timestamps else ""
        where = event.label or event.path

        if event.result is MISSING:
            self._write(f"{pad}{indent}{where}{suffix}")
            return

        injected = " (injected)" if event.injected else ""
        self._write(f"{pad}{indent}{where} -> {event.result!r}{injected}{suffix}")

    def on_error(self, event: Event) -> None:
        """Print the exception the operation raised."""

        indent = "  " * event.depth
        pad = " " * len(_wall_clock()) + " " if self._timestamps else ""
        where = event.label or event.path
        exception = type(event.exception).__name__
        self._write(f"{pad}{indent}{where} !! {exception}{self._suffix(event)}")

    def flush(self) -> None:
        """Flush the destination; lines are already flushed as written,
        so this only matters for a stream that buffers beneath that."""

        with self._lock:
            stream = self._file if self._file is not None else self._stream
            if stream is not None:
                stream.flush()

    def reopen(self) -> None:
        """Close the current file and open whatever the path template
        names now, so a template with a time variable moves on to a new
        file. Called by rotate= on its interval; call it directly from
        a signal handler for rotation on demand. A no-op for a stream,
        before the first line, or after close()."""

        with self._lock:
            if self._path is None or self._closed or self._file is None:
                return

            try:
                self._file.close()
                self._file = None
                self._open()
            except Exception:
                _note_sink_error(self)

    def release(self) -> None:
        """Close the current file and hold none until the next line,
        which opens the path afresh: the between-runs state of a
        per-run stream inside a window. A no-op for a stream, before
        the first line, or after close()."""

        with self._lock:
            if self._path is None or self._closed or self._file is None:
                return

            try:
                self._file.close()
            except Exception:
                _note_sink_error(self)
            self._file = None
            self._current = None

    def close(self) -> None:
        """Close the file opened for `path`; lines printed after this
        are dropped. Idempotent, and a no-op for a stream, which the
        caller owns."""

        with self._lock:
            if self._path is None or self._closed:
                return

            self._closed = True
            if self._schedule is not None:
                self._schedule.cancel()
            if self._file is not None:
                self._file.close()
                self._file = None


def _wall_clock() -> str:
    # Local time to the millisecond, for the printer's timestamps. The
    # date is not repeated per line; a file named by date carries it.

    now = time.time()
    local = time.localtime(now)
    return f"{time.strftime('%H:%M:%S', local)}.{int((now % 1) * 1000):03d}"


def _forward(sink: Sink, notification: str, event: Event) -> None:
    # Combinators forward with the same isolation the registry gives
    # top-level sinks, so the count and the warning land on the sink
    # that actually broke, and its siblings still get the event.

    try:
        getattr(sink, notification)(event)
    except Exception:
        _note_sink_error(sink)


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
    if event.phase is not None:
        record["phase"] = event.phase
    if event.stack is not None:
        record["stack"] = event.stack
    if event.data:
        record["data"] = _jsonable(event.data)

    return record


_STOP = object()
_REOPEN = object()
_RELEASE = object()


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

    `path` is an output path template ({date}, {time}, {pid}, {name}
    and the rest; see wrapture.outputs), expanded when the file is
    opened and its parent directories created. reopen() expands it
    again, which with a time variable in the path moves on to a new
    file; `rotate` calls reopen() on that interval ("1h", "15m", or
    seconds) and `align` puts the interval on the local wall-clock
    boundary. `name` is what {name} expands to.

    Declares "summary" capture on both axes: a streaming sink must
    neither retain live objects nor fail on unserialisable ones.
    Values that reach it captured by reference regardless (a binding
    override, another sink's requirement) are reduced to summaries at
    serialisation time.
    """

    capture_args: CapturePolicy | str = "summary"
    capture_result: CapturePolicy | str = "summary"

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        name: str = "jsonlines",
        limit: int = 1000,
        rotate: str | int | float | None = None,
        align: bool = False,
    ) -> None:
        self._path = OutputPath(path, name=name)
        self._interval, self._align = _rotation(self, self._path, rotate, align)
        self._schedule: Schedule | None = None

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=limit)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._dropped = 0
        self._current: str | None = None

    def __repr__(self) -> str:
        if self._current is None:
            return f"JSONLines({self._path.template!r})"
        return f"JSONLines({self._path.template!r}, writing={self._current!r})"

    @property
    def path(self) -> str | None:
        """The file currently being written, or None before the first
        line opens one."""

        with self._lock:
            return self._current

    def _open(self) -> TextIO:
        # Expand the template afresh and open what it names, remembering
        # the result so the sink can say where it is writing.

        current = self._path.expand()
        stream = open_output(current, "a")

        with self._lock:
            self._current = current

        return stream

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

                # The rotation schedule starts with the writer, so a
                # constructed but unused sink owns no timer either.

                if self._interval is not None:
                    self._schedule = every(
                        self.reopen, self._interval, align=self._align, name=repr(self)
                    )

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
        # The writer owns the file. It is opened on the first line and
        # again after a release, so a per-run stream inside a window
        # holds no file between runs. A failed open is counted and the
        # line dropped, and the writer keeps draining so flush()
        # barriers never hang and the application never blocks.

        stream: TextIO | None = None

        def close_stream() -> None:
            nonlocal stream
            if stream is None:
                return
            try:
                stream.flush()
                stream.close()
            except Exception:
                _note_sink_error(self)
            stream = None

        try:
            while True:
                item = self._queue.get()

                if item is _STOP:
                    break

                if item is _RELEASE:
                    close_stream()
                    with self._lock:
                        self._current = None
                    continue

                if item is _REOPEN:
                    # Release the current file and open whatever the
                    # template names now, so a time variable in the path
                    # moves on to a new file.

                    close_stream()
                    try:
                        stream = self._open()
                    except Exception:
                        _note_sink_error(self)
                    continue

                if isinstance(item, threading.Event):
                    if stream is not None:
                        try:
                            stream.flush()
                        except Exception:
                            _note_sink_error(self)
                    item.set()
                    continue

                if stream is None:
                    try:
                        stream = self._open()
                    except Exception:
                        _note_sink_error(self)
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
            close_stream()

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

    def reopen(self) -> None:
        """Close the current file and open whatever the path template
        names now.

        With a time variable in the path this moves on to a new file,
        which is how rotation works: rotate= calls it on its interval,
        and a signal handler can call it for rotation on demand.
        Queued lines drain to the old file first, so nothing is lost
        across the switch. Safe from any thread; a no-op before the
        first line or after close().
        """

        with self._lock:
            if not self._started or self._closed:
                return

        try:
            self._queue.put(_REOPEN, timeout=5)
        except queue.Full:
            pass

    def release(self) -> None:
        """Close the current file and hold none until the next line,
        which opens the path afresh.

        The between-runs state of a per-run stream inside a window:
        the writer thread stays, only the file is let go, so nothing
        is held open while the window is closed. Queued lines drain
        first. A no-op before the first line or after close().
        """

        with self._lock:
            if not self._started or self._closed:
                return

        try:
            self._queue.put(_RELEASE, timeout=5)
        except queue.Full:
            pass

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

            if self._schedule is not None:
                self._schedule.cancel()

        if not started or thread is None:
            return

        # The stop marker queues behind every pending line, so the
        # writer drains the lot before it exits and closes the file.

        try:
            self._queue.put(_STOP, timeout=5)
        except queue.Full:
            return

        thread.join(timeout=5)
