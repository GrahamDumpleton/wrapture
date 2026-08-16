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
import threading

from .capture import (
    NONE,
    REFERENCE,
    CapturePolicy,
    _level_of,
    _resolve_policy,
)
from .events import Event


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

    `capture_args` and `capture_result` declare the capture levels this
    sink needs of the values on events it receives. The effective level
    for a recorded event is the highest any active sink declares, so
    one sink's modest needs never downgrade another's.
    """

    capture_args: CapturePolicy | str = REFERENCE
    capture_result: CapturePolicy | str = REFERENCE

    def on_enter(self, event: Event) -> None:
        """Called when an event is recorded, before the operation runs."""

    def on_exit(self, event: Event) -> None:
        """Called when the operation behind the event completes."""

    def on_error(self, event: Event) -> None:
        """Called when the operation behind the event raises; the
        event's `exception` field holds what it raised."""


# The two tiers. Process sinks are deliberately a plain list rather
# than a contextvar, because a sink meant to observe the whole process
# must see events from every thread and task.

_process_sinks: list[Sink] = []
_scoped_sinks: contextvars.ContextVar[tuple[Sink, ...]] = contextvars.ContextVar(
    "wrapture_scoped_sinks", default=()
)


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


def _record_event(event: Event, active: tuple[Sink, ...]) -> Event:
    # Identity first, then delivery: a sink may read event.seq, and any
    # child recorded during the operation links to it.

    event.seq = _next_seq()

    guard = _in_recorder.set(True)
    try:
        for sink in active:
            sink.on_enter(event)
    finally:
        _in_recorder.reset(guard)

    return event


def _notify_exit(event: Event, active: tuple[Sink, ...]) -> None:
    # Completion goes to the sinks that saw the event enter, not to
    # whatever is active now: a coroutine or generator may complete in
    # a different context from the call that created it, and pairing
    # must not depend on that.

    guard = _in_recorder.set(True)
    try:
        for sink in active:
            sink.on_exit(event)
    finally:
        _in_recorder.reset(guard)


def _notify_error(event: Event, active: tuple[Sink, ...]) -> None:
    guard = _in_recorder.set(True)
    try:
        for sink in active:
            sink.on_error(event)
    finally:
        _in_recorder.reset(guard)


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
