"""Stack capture: recording how control physically reached a call.

The tape's parent and children links give the logical path between
observed points; they say nothing about the unobserved frames in
between. Stack capture answers "how did control actually get here",
priced per binding: no capture by default, just the immediate caller
for a few hundred nanoseconds, a fixed number of frames, or the full
stack when the whole route matters.

Captured stacks are interned. Stacks repeat almost perfectly (a
thousand captures at one call site typically produce one unique stack),
so each event stores a small integer id and the unique frame tuples
live in one side table. Frames are extracted to plain tuples
immediately and frame objects are never retained, since a held frame
keeps every local variable in it alive.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from typing import Final, NamedTuple

import wrapt


class StackFrame(NamedTuple):
    """One captured frame: where in the source control was."""

    filename: str
    lineno: int
    function: str


# The depths behind the stack= argument to binding(): "caller" captures
# just the calling frame, "full" every frame, and any positive integer
# captures that many frames.

caller: Final[int] = 1
full: Final[int] = sys.maxsize

_DEPTH_NAMES = {"caller": caller, "full": full}


def _resolve_depth(stack: int | str | None) -> int | None:
    if isinstance(stack, str):
        try:
            return _DEPTH_NAMES[stack]
        except KeyError:
            raise ValueError(
                f"stack must be None, 'caller', 'full' or a positive"
                f" frame count, got {stack!r}"
            ) from None

    return stack


# Frames of the observation machinery itself are elided, so a captured
# stack starts at the code under observation.

_ELIDED = (
    os.path.dirname(os.path.abspath(__file__)),
    os.path.dirname(os.path.abspath(wrapt.__file__)),
)

_lock = threading.Lock()
_ids: dict[tuple[StackFrame, ...], int] = {}
_stacks: list[tuple[StackFrame, ...]] = []

# The bound on unique interned stacks. Stacks repeat almost perfectly,
# so thousands of uniques means pathological churn (generated code,
# say) rather than normal operation; past the bound, captures intern
# one shared overflow marker instead of growing without limit, so the
# table's memory is bounded for the life of the process.

_STACK_LIMIT = 10000

_OVERFLOW = (StackFrame("<overflow>", 0, "<stack table full>"),)


def stack_frames(stack_id: int) -> tuple[StackFrame, ...]:
    """The frames behind an event's interned stack id, innermost first."""

    with _lock:
        try:
            return _stacks[stack_id]
        except IndexError:
            raise KeyError(
                f"stack id {stack_id!r} is not interned; the stack table"
                f" may have been cleared since the event was recorded"
            ) from None


def clear_stacks() -> None:
    """Empty the interned stack table, releasing its memory.

    For long-running processes that capture stacks: interning is
    bounded, but the bound is generous, and a natural flush point (a
    trace file rotated away, a tape discarded) can hand the memory
    back early. Stack ids on events recorded before the clear stop
    resolving; stack_frames() raises KeyError for them. Clear only
    when previously recorded events will no longer be consulted.
    """

    with _lock:
        _ids.clear()
        _stacks.clear()


def _intern(frames: tuple[StackFrame, ...]) -> int:
    with _lock:
        found = _ids.get(frames)
        if found is not None:
            return found

        # At the bound, the overflow marker stands in for any new
        # unique stack; it interns itself through the normal path the
        # first time, one slot past the limit.

        if len(_stacks) >= _STACK_LIMIT and frames != _OVERFLOW:
            frames = _OVERFLOW
            found = _ids.get(frames)
            if found is not None:
                return found

        found = len(_stacks)
        _ids[frames] = found
        _stacks.append(frames)
        return found


def _capture(depth: int) -> int:
    frames: list[StackFrame] = []
    frame: types.FrameType | None = sys._getframe(1)

    try:
        while frame is not None and len(frames) < depth:
            code = frame.f_code
            filename = code.co_filename

            if not filename.startswith(_ELIDED):
                frames.append(StackFrame(filename, frame.f_lineno, code.co_qualname))

            frame = frame.f_back
    finally:
        # Never retain a frame object past the walk.

        del frame

    return _intern(tuple(frames))
