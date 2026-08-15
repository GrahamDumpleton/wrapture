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


# The stack= argument to binding(): capture just the calling frame, or
# every frame. Any other positive integer captures that many frames.

caller: Final[int] = 1
full: Final[int] = sys.maxsize

# Frames of the observation machinery itself are elided, so a captured
# stack starts at the code under observation.

_ELIDED = (
    os.path.dirname(os.path.abspath(__file__)),
    os.path.dirname(os.path.abspath(wrapt.__file__)),
)

_lock = threading.Lock()
_ids: dict[tuple[StackFrame, ...], int] = {}
_stacks: list[tuple[StackFrame, ...]] = []


def stack_frames(stack_id: int) -> tuple[StackFrame, ...]:
    """The frames behind an event's interned stack id, innermost first."""

    with _lock:
        return _stacks[stack_id]


def _intern(frames: tuple[StackFrame, ...]) -> int:
    with _lock:
        found = _ids.get(frames)

        if found is None:
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
