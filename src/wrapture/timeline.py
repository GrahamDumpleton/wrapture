"""The recording scope: the tape a test asserts against.

A timeline is the scoped form of listening: entering pushes a Tape
onto the scoped sink tier, so events recorded inside the block land on
it, and exiting removes it again. The tape is one sink among many; the
gate that decides whether anything records at all lives in the sinks
module.

The in-progress call stack also lives here, in a context variable, set
when a timeline is entered and restored on exit. A context variable
specifically, because a module-level global would be shared by
concurrent asyncio tasks, recording one task's calls as children of
another's, and a thread-local would fail the same way for many tasks
on one thread. Each task gets its own copy of the context, so each
records its own correctly nested tree.
"""

from __future__ import annotations

import contextvars
import functools
import threading
from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable

from wrapt import MISSING

from .capture import NONE, REFERENCE, CapturePolicy, _capture_value, _level_of
from .eventlogs import EventLog
from .events import Event, _format_time, _own_time
from .sinks import Sink, _in_recorder, _scoped_sinks


@runtime_checkable
class _Appliable(Protocol):
    # Anything with the binding lifecycle: a Binding or a BindingGroup.
    # Duck-typed rather than imported, because the bindings module is a
    # consumer of this one and importing it here would be circular.

    def apply(self) -> Any: ...

    def remove(self) -> Any: ...


class Tape(Sink):
    """The retained record of events for one timeline.

    The tape is the sink the testing workflow is built on: it keeps
    every event it hears about and serves the filtered views and
    assertions. Sequence numbers are allocated process-wide by the
    recording machinery, so ordering assertions have a single
    authoritative order even when events arrive from concurrently
    running tasks.
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
        self._closed = False
        self._discarded = 0

    def on_enter(self, event: Event) -> None:
        """Retain the event. It stays live: the recorder fills in the
        outcome fields as the operation completes.

        A closed tape discards instead: an event arriving after the
        enclosing timeline exited (from a task or thread that outlived
        the scope) is counted on `discarded` rather than appended, so
        a result cannot grow after it has been asserted on, and the
        loss is a visible fact rather than a silent race.
        """

        with self._lock:
            if self._closed:
                self._discarded += 1
                return

            self._entries.append(event)

    @property
    def closed(self) -> bool:
        """True after the enclosing timeline exits, until it is next
        entered. A closed tape still serves every view and assertion;
        it just no longer accepts new events."""

        with self._lock:
            return self._closed

    @property
    def discarded(self) -> int:
        """Events that arrived after the tape closed and were dropped.

        A non-zero count means work observed by this timeline was still
        running when the scope exited; wrapture.propagate() and the
        known limitations page cover how that happens.
        """

        with self._lock:
            return self._discarded

    @property
    def pending(self) -> int:
        """Events on the tape whose operation has not ended: calls in
        flight, generators not yet closed, coroutines never awaited.
        Live, so a coroutine awaited after the scope exits leaves the
        count; while the count stays non-zero after the code under test
        has finished, something was called and never awaited, or never
        run to completion.
        """

        with self._lock:
            return sum(1 for event in self._entries if not event.finished)

    def _open(self) -> None:
        with self._lock:
            self._closed = False

    def _close(self) -> None:
        with self._lock:
            self._closed = True

    def __repr__(self) -> str:
        with self._lock:
            count = len(self._entries)
            discarded = self._discarded
            pending = sum(1 for event in self._entries if not event.finished)

        plural = "" if count == 1 else "s"
        notes = []
        if pending:
            notes.append(f"{pending} pending")
        if discarded:
            notes.append(f"{discarded} discarded after close")

        if notes:
            return f"<Tape: {count} event{plural}, {', '.join(notes)}>"
        return f"<Tape: {count} event{plural}>"

    def _snapshot(self) -> list[Event]:
        # A consistent view in sequence order. Arrival order can differ
        # slightly under threads, because sequence allocation and
        # delivery are two steps; sorting restores the authoritative
        # order, and is near-linear on an almost-sorted list.

        with self._lock:
            return sorted(self._entries, key=lambda event: event.seq)

    @property
    def all(self) -> list[Event]:
        """Every recorded event, in sequence order."""

        return self._snapshot()

    def for_binding(self, bnd: Any) -> EventLog:
        """A filterable view over this tape's events for one binding."""

        events = [event for event in self._snapshot() if event.binding is bnd]

        return EventLog(getattr(bnd, "label", repr(bnd)), events)

    def roots(self) -> list[Event]:
        """The top-level events: those recorded with no observed caller."""

        return [event for event in self._snapshot() if event.parent_id is None]

    def parent_of(self, event: Event) -> Event | None:
        """The event the given one was recorded inside, or None for a
        root.

        Events link to their parent by sequence number rather than by
        reference; this resolves the link back to the event object.
        """

        if event.parent_id is None:
            return None

        with self._lock:
            for entry in self._entries:
                if entry.seq == event.parent_id:
                    return entry

        return None

    def children_of(self, event: Event) -> list[Event]:
        """The events recorded directly inside the given one, in
        recording order."""

        return [entry for entry in self._snapshot() if entry.parent_id == event.seq]

    def self_time(self, event: Event) -> float | None:
        """The time spent in the operation itself: its execution time
        minus its observed children's, the figure profilers rank by.

        The basis is the event's duration, except for a generator,
        whose wall duration includes the consumer's time between
        yields; its accumulated body time is used instead, and the
        same rule applies to the children being subtracted. Returns
        None when the event has not closed with a time.
        """

        own = _own_time(event)
        if own is None:
            return None

        spent = 0.0
        for child in self.children_of(event):
            child_time = _own_time(child)
            if child_time is not None:
                spent += child_time

        return max(0.0, own - spent)

    def tree(self, *, times: bool = False) -> str:
        """The call graph as it actually ran, one event per line,
        indented by nesting depth.

        A completed event shows its result after `->`; one that raised
        shows `!!` and the exception type; one still in progress shows
        neither. With times=True a timed event also shows its
        execution time and, where observed children account for part
        of it, its self time.
        """

        entries = self._snapshot()

        # Rebuild the nesting from the id links: roots in recording
        # order, and each event's children grouped under its seq.

        roots: list[Event] = []
        children: dict[int, list[Event]] = {}

        for event in entries:
            if event.parent_id is None:
                roots.append(event)
            else:
                children.setdefault(event.parent_id, []).append(event)

        lines: list[str] = []

        def timing(event: Event) -> str:
            own = _own_time(event)
            if own is None:
                return ""

            spent = 0.0
            for child in children.get(event.seq, []):
                child_time = _own_time(child)
                if child_time is not None:
                    spent += child_time

            if spent > 0.0:
                self_time = max(0.0, own - spent)
                return f"  [{_format_time(own)}, self {_format_time(self_time)}]"
            return f"  [{_format_time(own)}]"

        def emit(event: Event) -> None:
            injected = " (injected)" if event.injected else ""

            # A get event's str() already carries its value, so only the
            # injected mark is added to it.

            if event.exception is not None:
                marker = f"  !! {type(event.exception).__name__}{injected}"
            elif event.kind == "get":
                marker = injected
            elif event.result is not MISSING:
                marker = f"  -> {event.result!r}{injected}"
            else:
                marker = ""

            line = "  " * event.depth + str(event) + marker
            if times:
                line += timing(event)

            lines.append(line)

            for child in children.get(event.seq, []):
                emit(child)

        for root in roots:
            emit(root)

        return "\n".join(lines)

    def assert_order(
        self, *steps: Any, consecutive: bool = False, exact: bool = False
    ) -> Tape:
        """Assert the tape recorded events matching the steps, in order.

        Each step is a binding or an observed callable (stub() and
        mock() methods included), accepting any event it recorded, or
        an EventLog, accepting exactly the events it holds, so a filtered
        log is the way to say which call: `charge.events.with_args(
        amount=500)`, `tape.for_binding(record).raising(TimeoutError)`.
        The kinds mix freely, and repeating a step requires that
        many matching events in order.

        By default this is a subsequence check: other events may appear
        before, between and after, and only the relative order matters.
        The flags tighten it, each about the bindings the steps name
        (events of any other binding are invisible to the assertion):
        `consecutive=True` requires the steps to match a consecutive run
        of those bindings' events, nothing of theirs between;
        `exact=True` requires those bindings' events to be exactly the
        steps, nothing before or after either, and implies consecutive.
        Raises AssertionError saying where the expectation stalled or
        which event broke it, with the actual timeline. Returns the
        tape, so it chains.
        """

        matchers: list[Callable[[Event], bool]] = []
        labels: list[str] = []
        named: set[int] = set()

        def accepts_log(log: EventLog) -> Callable[[Event], bool]:
            wanted = {id(event) for event in log}
            return lambda event: id(event) in wanted

        def accepts_binding(wanted: Any) -> Callable[[Event], bool]:
            return lambda event: event.binding is wanted

        for step in steps:
            # An observed callable accessed as a bound method records
            # under its parent wrapper; resolve to that identity.

            recorder = getattr(step, "_self_parent", None) or step

            if isinstance(step, EventLog):
                matchers.append(accepts_log(step))
                labels.append(step.label)
                named.update(id(event.binding) for event in step)
            elif hasattr(step, "apply") or hasattr(recorder, "_self_path"):
                matchers.append(accepts_binding(recorder))
                labels.append(getattr(step, "label", repr(step)))
                named.add(id(recorder))
            else:
                raise TypeError(
                    f"assert_order() steps are bindings or event logs, got {step!r}"
                )

        consecutive = consecutive or exact
        entries = self._snapshot()
        total = len(steps)

        def actual() -> str:
            return "\n".join(f"    {event}" for event in entries) or "    (no events)"

        def step_name(index: int) -> str:
            return f"{labels[index]} (position {index + 1} of {total})"

        # One pass in record order. An event the current step accepts
        # advances the cursor; any other event is skipped, unless it
        # belongs to a named binding and the flags make it count.

        position = 0
        started = False

        for event in entries:
            if position < total and matchers[position](event):
                position += 1
                started = True
                continue

            if id(event.binding) not in named:
                continue

            if exact and not started:
                raise AssertionError(
                    f"expected exactly the given events; saw {event} before"
                    f" {step_name(0)}\n  actual timeline:\n{actual()}"
                )

            if consecutive and started and position < total:
                raise AssertionError(
                    f"expected consecutive events; after {step_name(position - 1)}"
                    f" saw {event} where {step_name(position)} was expected\n"
                    f"  actual timeline:\n{actual()}"
                )

            if exact and position == total:
                raise AssertionError(
                    f"expected exactly the given events; saw {event} after"
                    f" {step_name(total - 1)}\n  actual timeline:\n{actual()}"
                )

        if position != total:
            raise AssertionError(
                f"expected order not satisfied; stalled waiting for"
                f" {step_name(position)}\n  actual timeline:\n{actual()}"
            )

        return self


# The in-progress stack: the events currently open in this context,
# innermost last, and the entire source of parent links and depth. The
# recording switch is not here; it is "are any sinks active", answered
# by the sinks module.

_stack: contextvars.ContextVar[tuple[Event, ...]] = contextvars.ContextVar(
    "wrapture_stack", default=()
)


def _current_tape() -> Tape | None:
    # The innermost scoped Tape, for the views that speak about "the
    # enclosing timeline": Binding.events and the expectation verifier.
    # Other kinds of sink have no such views, so only tapes count.

    for sink in reversed(_scoped_sinks.get()):
        if isinstance(sink, Tape):
            return sink

    return None


# How many timelines are active process-wide, kept so a wrapper firing
# with no ambient tape can tell "nothing is recording anywhere" from
# "a timeline is running but this thread has no context". Threads start
# with a fresh context, so their calls otherwise vanish from the tape
# silently; see the known limitations page.

_active_lock = threading.Lock()
_active_count = 0


def _timeline_started() -> None:
    global _active_count

    with _active_lock:
        _active_count += 1


def _timeline_finished() -> None:
    global _active_count

    with _active_lock:
        _active_count -= 1


def _timelines_active() -> bool:
    return _active_count > 0


def _push(event: Event) -> contextvars.Token[tuple[Event, ...]]:
    # Nest the event under whatever is currently in progress, then make
    # it the innermost in-progress event. Nesting links by sequence
    # number, and the parent on the stack already carries its seq: its
    # delivery ran before its body, and nothing can record in between.
    # The event being pushed gets its own seq at delivery, immediately
    # after this push, so sinks hear on_enter with position complete.
    # The returned token restores the previous stack in _pop, which
    # must run in the same context.

    stack = _stack.get()
    parent = stack[-1] if stack else None

    event.parent_id = parent.seq if parent is not None else None
    event.depth = len(stack)

    return _stack.set(stack + (event,))


def _pop(token: contextvars.Token[tuple[Event, ...]]) -> None:
    _stack.reset(token)


def _capture_result(event: Event, outcome: Any, policy: CapturePolicy) -> None:
    # Result capture runs under the recorder guard: at SUMMARY and above
    # it calls user code (repr, deepcopy), which must not record.

    if _level_of(policy) <= NONE:
        return

    guard = _in_recorder.set(True)
    try:
        event.result = _capture_value(policy, None, outcome)
    finally:
        _in_recorder.reset(guard)


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

    Entering pushes the timeline's tape onto the scoped sinks and
    applies the bindings given at creation, rolling back if any of them
    fails to apply. Exiting removes them in reverse order, restores the
    previous ambient state, and closes the tape: an event arriving
    after that, from a task or thread that outlived the scope, is
    discarded and counted rather than appended. The same timeline can
    be reused sequentially; entering again reopens the tape, which
    keeps accumulating across uses.
    """

    def __init__(self, appliables: list[_Appliable]) -> None:
        self.tape = Tape()

        self._appliables = appliables
        self._applied: list[_Appliable] = []
        self._sinks_token: contextvars.Token[tuple[Sink, ...]] | None = None
        self._stack_token: contextvars.Token[tuple[Event, ...]] | None = None

    def __enter__(self) -> Tape:
        if self._sinks_token is not None:
            raise RuntimeError("timeline is already active")

        self._sinks_token = _scoped_sinks.set(_scoped_sinks.get() + (self.tape,))
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

        # Open (or on reuse, reopen) the tape only once entry cannot
        # fail, so a rolled-back reuse leaves it closed.

        self.tape._open()

        _timeline_started()
        return self.tape

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        _timeline_finished()

        # Close first: the recording scope is over, and anything still
        # running elsewhere should discard visibly from here on.

        self.tape._close()

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
        assert self._sinks_token is not None
        assert self._stack_token is not None

        _scoped_sinks.reset(self._sinks_token)
        _stack.reset(self._stack_token)

        self._sinks_token = None
        self._stack_token = None


def propagate(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Make fn record to this context's timeline from another thread.

    Threads do not inherit context variables, so work handed to a
    thread from inside a timeline is invisible to that timeline (see
    the known limitations page). propagate() captures the recording
    context where it is called and returns a callable that runs fn
    inside a copy of it, so the thread records exactly as the caller
    would:

        threading.Thread(target=wrapture.propagate(work)).start()

    Each invocation runs in its own copy of the captured context, so
    one propagated callable can be shared by several threads at once.
    A propagated thread that outlives the timeline is safe by
    construction: the tape closes when the scope exits, and anything
    the thread records after that is discarded and counted on the
    tape's `discarded` rather than appended.
    """

    context = contextvars.copy_context()
    lock = threading.Lock()

    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> Any:
        # Context.run is not reentrant, so each invocation gets its
        # own copy of the captured context, minted under a lock since
        # copying requires briefly entering the original.

        with lock:
            current = context.run(contextvars.copy_context)

        return current.run(fn, *args, **kwargs)

    return call


def timeline(*bindings: _Appliable | Iterable[_Appliable]) -> Timeline:
    """Open a recording scope.

    Bindings passed here are applied on entry and removed on exit: in
    a test nothing else is listening, so the recording scope and the
    useful patch lifetime are the same interval.
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
