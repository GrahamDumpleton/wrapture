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
import sys
import threading
import warnings
from typing import TextIO

from wrapt import MISSING

from .capture import (
    NONE,
    REFERENCE,
    CapturePolicy,
    _level_of,
    _resolve_policy,
)
from .events import Event
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
