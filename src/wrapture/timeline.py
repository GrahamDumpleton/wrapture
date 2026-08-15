"""The recording scope: the tape events land on, and how they find it.

Observed code calls its own methods normally; nothing threads a tape
through those calls. So when a wrapper fires it answers two questions
from ambient state: am I recording, and into what; and what call am I
nested inside. Both live in context variables, set when a timeline is
entered and restored on exit.

Context variables specifically, because a module-level global would be
shared by concurrent asyncio tasks, recording one task's calls as
children of another's, and a thread-local would fail the same way for
many tasks on one thread. Each task gets its own copy of the context,
so each records its own correctly nested tree.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from wrapt import MISSING

from .capture import REFERENCE, CapturePolicy
from .eventlogs import EventLog
from .events import Event


@runtime_checkable
class _Appliable(Protocol):
    # Anything with the binding lifecycle: a Binding or a BindingGroup.
    # Duck-typed rather than imported, because the bindings module is a
    # consumer of this one and importing it here would be circular.

    def apply(self) -> Any: ...

    def remove(self) -> Any: ...


class Tape:
    """The ordered record of events for one timeline.

    The tape assigns each event its sequence number as it is recorded,
    so ordering assertions have a single authoritative order even when
    events arrive from concurrently running tasks.
    """

    # The capture levels this sink requires of bindings that follow the
    # sink (design: the sink says what it needs). REFERENCE on both
    # axes, because a test asserts within the scope, where references
    # are accurate and cost nothing. A streaming sink would declare
    # SUMMARY arguments and NONE results instead.

    capture_args: CapturePolicy = REFERENCE
    capture_result: CapturePolicy = REFERENCE

    def __init__(self) -> None:
        self._entries: list[Event] = []
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, event: Event) -> Event:
        """Assign the next sequence number to the event and append it.

        Returns the event, which stays live: the recorder fills in the
        outcome fields when the call completes.
        """

        with self._lock:
            self._seq += 1
            event.seq = self._seq
            self._entries.append(event)

        return event

    @property
    def all(self) -> list[Event]:
        """Every recorded event, in sequence order."""

        with self._lock:
            return list(self._entries)

    def for_binding(self, bnd: Any) -> EventLog:
        """A filterable view over this tape's events for one binding."""

        with self._lock:
            events = [event for event in self._entries if event.binding is bnd]

        return EventLog(getattr(bnd, "label", repr(bnd)), events)

    def roots(self) -> list[Event]:
        """The top-level events: those recorded with no observed caller."""

        with self._lock:
            return [event for event in self._entries if event.parent is None]

    def tree(self) -> str:
        """The call graph as it actually ran, one event per line,
        indented by nesting depth.

        A completed event shows its result after `->`; one that raised
        shows `!!` and the exception type; one still in progress shows
        neither.
        """

        lines: list[str] = []

        def emit(event: Event) -> None:
            injected = " (injected)" if event.injected else ""

            if event.exception is not None:
                marker = f"  !! {type(event.exception).__name__}{injected}"
            elif event.result is not MISSING:
                marker = f"  -> {event.result!r}{injected}"
            else:
                marker = ""

            lines.append("  " * event.depth + str(event) + marker)

            for child in event.children:
                emit(child)

        for root in self.roots():
            emit(root)

        return "\n".join(lines)

    def assert_order(self, *bindings: Any) -> Tape:
        """Assert the bindings recorded events in the given order.

        A subsequence check, not an exact match: other events may appear
        before, between and after, and only the relative order of the
        given bindings' events matters. Repeating a binding requires it
        to have recorded that many times in order. Raises AssertionError
        naming where the expectation stalled, with the actual timeline.
        """

        with self._lock:
            entries = list(self._entries)

        position = 0
        for event in entries:
            if position < len(bindings) and event.binding is bindings[position]:
                position += 1

        if position != len(bindings):
            stalled = getattr(bindings[position], "label", repr(bindings[position]))
            actual = "\n".join(f"    {event}" for event in entries) or "    (no events)"
            raise AssertionError(
                f"expected order not satisfied; stalled waiting for"
                f" {stalled} (position {position + 1} of {len(bindings)})\n"
                f"  actual timeline:\n{actual}"
            )

        return self


# The ambient state. The tape variable doubles as the recording switch:
# None means no timeline is active and wrappers call straight through.
# The stack variable holds the events currently in progress, innermost
# last, and is the entire source of parent, depth and children.

_tape: contextvars.ContextVar[Tape | None] = contextvars.ContextVar(
    "wrapture_tape", default=None
)
_stack: contextvars.ContextVar[tuple[Event, ...]] = contextvars.ContextVar(
    "wrapture_stack", default=()
)

# The reentrancy guard. Set while the recording machinery itself runs, so
# an observed callable invoked from inside the recorder (rather than from
# the code under observation) does not record recursively without bound.
# Behaviour still applies on the guarded path: only recording is skipped,
# so a call the user stubbed out stays stubbed even when the recorder
# triggers it.

_in_recorder: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "wrapture_in_recorder", default=False
)


def _push(event: Event) -> contextvars.Token[tuple[Event, ...]]:
    # Nest the event under whatever is currently in progress, then make
    # it the innermost in-progress event. The returned token restores
    # the previous stack in _pop, which must run in the same context.

    stack = _stack.get()
    parent = stack[-1] if stack else None

    event.parent = parent
    event.depth = len(stack)
    if parent is not None:
        parent.children.append(event)

    return _stack.set(stack + (event,))


def _pop(token: contextvars.Token[tuple[Event, ...]]) -> None:
    _stack.reset(token)


def current_event() -> Event | None:
    """The in-flight event, or None when nothing is being recorded.

    The behaviour pipeline runs after its event is pushed, so this is
    reachable from inside a decorates() handler, where it names the
    event for the very call the handler is wrapping.
    """

    stack = _stack.get()
    return stack[-1] if stack else None


def annotate(**data: Any) -> None:
    """Merge values into the in-flight event's data dict.

    Annotation is targeted capture: the caller attaches what it knows a
    generic policy cannot infer (a row count, a cache hit, an immutable
    copy of a value that will be mutated). A silent no-op when nothing
    is being recorded, so observed code can call it unconditionally.
    """

    event = current_event()
    if event is not None:
        event.data.update(data)


class Timeline:
    """A recording scope, created by timeline().

    Entering sets the ambient tape and applies the bindings given at
    creation, rolling back if any of them fails to apply. Exiting
    removes them in reverse order and restores the previous ambient
    state. The same timeline can be reused sequentially; its tape keeps
    accumulating across uses.
    """

    def __init__(self, appliables: list[_Appliable]) -> None:
        self.tape = Tape()

        self._appliables = appliables
        self._applied: list[_Appliable] = []
        self._tape_token: contextvars.Token[Tape | None] | None = None
        self._stack_token: contextvars.Token[tuple[Event, ...]] | None = None

    def __enter__(self) -> Tape:
        if self._tape_token is not None:
            raise RuntimeError("timeline is already active")

        self._tape_token = _tape.set(self.tape)
        self._stack_token = _stack.set(())

        # Apply every binding, rolling the whole entry back if one
        # fails, so a partially patched scope never survives.

        try:
            for appliable in self._appliables:
                appliable.apply()
                self._applied.append(appliable)
        except Exception:
            for applied in reversed(self._applied):
                applied.remove()
            self._applied.clear()

            self._restore()
            raise

        return self.tape

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for applied in reversed(self._applied):
            applied.remove()
        self._applied.clear()

        self._restore()

        # Verify declared expectations only when the block itself
        # succeeded: raising here over an in-flight failure would bury
        # the real cause.

        if exc_type is None:
            for appliable in self._appliables:
                verify = getattr(appliable, "_verify", None)
                if verify is not None:
                    verify(self.tape)

    def _restore(self) -> None:
        assert self._tape_token is not None
        assert self._stack_token is not None

        _tape.reset(self._tape_token)
        _stack.reset(self._stack_token)

        self._tape_token = None
        self._stack_token = None


def timeline(*bindings: _Appliable | Iterable[_Appliable]) -> Timeline:
    """Open a recording scope.

    Bindings passed here are applied on entry and removed on exit: a
    binding applied outside a timeline records nothing anyway, so the
    recording scope and the useful patch lifetime are the same interval.
    Accepts bindings, binding groups, or iterables of either; a group is
    applied as a unit, keeping its own rollback behaviour. With no
    arguments the scope only records, for bindings whose lifetime is
    managed elsewhere.
    """

    flattened: list[_Appliable] = []

    for entry in bindings:
        if isinstance(entry, _Appliable):
            flattened.append(entry)
        else:
            flattened.extend(entry)

    return Timeline(flattened)
