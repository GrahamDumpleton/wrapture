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
import fnmatch
import functools
import sys
import threading
import time
import warnings
import weakref
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, Self, runtime_checkable

from wrapt import MISSING

from . import trace as _trace
from .capture import NONE, REFERENCE, CapturePolicy, _capture_value, _level_of
from .eventlogs import EventLog
from .events import CaughtException, Event, _caught_types, _format_time, _own_time
from .exceptions import ConfigWarning
from .sinks import (
    Sink,
    _active_sinks,
    _in_recorder,
    _notify_error,
    _notify_exit,
    _record_event,
    _scoped_sinks,
)


@runtime_checkable
class _Appliable(Protocol):
    # Anything with the binding lifecycle: a Binding or a BindingGroup.
    # Duck-typed rather than imported, because the bindings module is a
    # consumer of this one and importing it here would be circular.

    def apply(self) -> Any: ...

    def remove(self) -> Any: ...


class _EventQueries:
    """The query face shared by the tape and its subtree views.

    Every reader here is written against one primitive, _snapshot(),
    a consistent list of the view's member events in sequence order;
    a concrete class supplies it (the tape from its retained entries,
    a subtree view by walking down from its root event), and
    membership is what scopes every filter, tree and assertion.
    """

    def _snapshot(self) -> list[Event]:
        raise NotImplementedError

    @property
    def all(self) -> list[Event]:
        """Every recorded event, in sequence order."""

        return self._snapshot()

    def for_binding(self, bnd: Any) -> EventLog:
        """A filterable view over this tape's events for one binding.

        Accepts the binding itself or a behaviour namespace standing in
        for it, as a chain like `binding(...).on_call.returns(None)`
        hands back.
        """

        bnd = getattr(bnd, "_binding", bnd)

        events = [event for event in self._snapshot() if event.binding is bnd]

        where = getattr(bnd, "label", None) or getattr(bnd, "path", None) or repr(bnd)
        return EventLog(where, events)

    def where(self, *, path: str | None = None, label: str | None = None) -> EventLog:
        """A filterable view over this tape's events selected by the
        strings they carry, for when no binding is in hand.

        `path` matches an event's path exactly, the module:qualname of
        the target as `Binding.path` spells it. `label` matches the
        name an event is shown under: its assigned label, or its path
        when it has none. Given together both have to match; one of
        them is required. find_binding() is the usual route, since it
        hands back the binding itself; this is the fallback for events
        whose binding is not obtainable.
        """

        if path is None and label is None:
            raise ValueError("where() needs a path, a label, or both")

        events = [
            event
            for event in self._snapshot()
            if (path is None or event.path == path)
            and (label is None or (event.label or event.path) == label)
        ]

        return EventLog(label if label is not None else str(path), events)

    def blocks(self, name: str = "*") -> EventLog:
        """A filterable view over this tape's block events, selected by
        name.

        `name` is an fnmatch-style pattern matched case-sensitively
        against each block's name, the same pattern language the config
        filters use; the default selects every block. The result is an
        EventLog, so the whole filter and assertion surface applies,
        and it feeds assert_order() steps, where a block orders by its
        entry.
        """

        events = [
            event
            for event in self._snapshot()
            if event.kind == "block"
            and event.label is not None
            and fnmatch.fnmatchcase(event.label, name)
        ]

        return EventLog(f"block: {name}", events)

    def within(self, event: Event) -> Subtree:
        """A tape-like view over the events recorded inside the given
        one: its descendants, not the event itself.

        The whole query face applies, scoped: for_binding() and
        blocks() select among the contents, assert_order() never sees
        an event outside them, roots() lists the direct children, and
        tree() draws the branch from the margin. The view is live,
        like the tape it came from, and views nest: within() on a
        view scopes further down.
        """

        return Subtree(self, event)

    def roots(self) -> list[Event]:
        """The top-level events of this view: those whose parent is
        not a member.

        On the whole tape that means events recorded with no observed
        caller, plus any recorded under a parent the tape never heard
        of (an operation already in flight when the timeline was
        entered); on a subtree view it means the root event's direct
        children.
        """

        entries = self._snapshot()
        members = {event.seq for event in entries}

        return [
            event
            for event in entries
            if event.parent_id is None or event.parent_id not in members
        ]

    def parent_of(self, event: Event) -> Event | None:
        """The event the given one was recorded inside, or None for a
        root.

        Events link to their parent by sequence number rather than by
        reference; this resolves the link back to the event object.
        """

        if event.parent_id is None:
            return None

        for entry in self._snapshot():
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
        neither. An exception caught inside the scope and noted with
        note_exception() shows as `!!` and its type after the result,
        so one line can say both that the scope returned and that it
        failed. With times=True a timed event also shows its
        execution time and, where observed children account for part
        of it, its self time.
        """

        entries = self._snapshot()

        # Rebuild the nesting from the id links: events whose parent
        # is not in the view are the roots, in recording order, and
        # each event's children are grouped under its seq. Indentation
        # comes from the traversal rather than the recorded depth, so
        # a subtree view starts at the margin and an event whose
        # parent was never heard of lines up as the root it stands as.

        members = {event.seq for event in entries}

        roots: list[Event] = []
        children: dict[int, list[Event]] = {}

        for event in entries:
            if event.parent_id is None or event.parent_id not in members:
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

        def emit(event: Event, level: int) -> None:
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

            # Exceptions caught inside the scope and noted against it
            # follow the outcome, one marker each, in the order noted.

            marker += "".join(f"  !! {name}" for name in _caught_types(event))

            line = "  " * level + str(event) + marker
            if times:
                line += timing(event)

            lines.append(line)

            for child in children.get(event.seq, []):
                emit(child, level + 1)

        for root in roots:
            emit(root, 0)

        return "\n".join(lines)

    def assert_order(
        self, *steps: Any, consecutive: bool = False, exact: bool = False
    ) -> Self:
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
        which event broke it, with the actual timeline. Returns this
        tape or view, so it chains.
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
            # under its parent wrapper; resolve to that identity. A
            # behaviour namespace stands in for its binding, so resolve
            # that too.

            recorder = getattr(step, "_self_parent", None) or step
            recorder = getattr(recorder, "_binding", recorder)

            if isinstance(step, EventLog):
                matchers.append(accepts_log(step))
                labels.append(step.label)
                named.update(id(event.binding) for event in step)
            elif hasattr(step, "apply") or hasattr(recorder, "_self_path"):
                matchers.append(accepts_binding(recorder))
                labels.append(
                    getattr(step, "label", None)
                    or getattr(step, "path", None)
                    or repr(step)
                )
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


class Tape(_EventQueries, Sink):
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


class Subtree(_EventQueries):
    """A tape-like view over the events recorded inside one event,
    created by within().

    The container event is not a member of its own view (within means
    contents): the view's roots are its direct children, the container
    itself is exposed as `root`, and parent_of() on a direct child
    returns the real container event, honest at the boundary rather
    than clipped to membership. The view is live, recomputing its
    membership from the underlying tape per access, and carries only
    the query face; the recording-scope facts (closed, discarded,
    pending) belong to the tape.
    """

    def __init__(self, source: _EventQueries, root: Event) -> None:
        self._source = source
        self._root = root

    @property
    def root(self) -> Event:
        """The container event this view holds the contents of."""

        return self._root

    def _snapshot(self) -> list[Event]:
        # Take the source's snapshot, index children by parent, and
        # walk down from the root; sorting restores one sequence order
        # across branches.

        entries = self._source._snapshot()

        children: dict[int, list[Event]] = {}
        for event in entries:
            if event.parent_id is not None:
                children.setdefault(event.parent_id, []).append(event)

        descendants: list[Event] = []
        queue: list[int] = [self._root.seq]

        while queue:
            seq = queue.pop()
            for child in children.get(seq, []):
                descendants.append(child)
                queue.append(child.seq)

        return sorted(descendants, key=lambda event: event.seq)

    def parent_of(self, event: Event) -> Event | None:
        """The event the given one was recorded inside; for a direct
        child of the view's root this is the root event itself."""

        if event.parent_id is not None and event.parent_id == self._root.seq:
            return self._root

        return super().parent_of(event)

    def __repr__(self) -> str:
        count = len(self._snapshot())
        plural = "" if count == 1 else "s"
        return f"<Subtree of {self._root}: {count} event{plural}>"


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

    # Trace identity. An event arriving with a context already set (a
    # request boundary that parsed incoming headers) keeps it, shading
    # its subtree; otherwise a nested event shares its parent's by
    # reference, and a root mints one when the mechanism is enabled,
    # process-wide or by this root's binding. Minting is gated by
    # kind: traces start at declared operation boundaries (a function
    # invoked, a request arriving, a block entered), so a root
    # attribute access never starts one.

    if event.trace is None:
        if parent is not None:
            event.trace = parent.trace
        elif event.kind in ("call", "request", "block") and (
            _trace._active() or getattr(event.binding, "_trace_root", False)
        ):
            event.trace = _trace.mint()

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


class EventHandle:
    """A handle on an in-flight event, returned by current_event().

    Reading an attribute reads the underlying Event's field, and the
    handle adds the two mutation verbs that are valid while an event
    is in flight: annotate() and note_exception(). An event read back
    from a tape afterwards is a plain Event with no mutation surface;
    the handle is how code inside an operation speaks about the
    operation, not a way to edit history.

    A handle is empty when current_event() matched nothing. An empty
    handle is falsy, its verbs silently do nothing, and reading a
    field from it raises AttributeError naming the filters that
    failed to match. Truthiness is the test for inspection code:

        if current_event(kind="request"):
            ...

    while the verbs need no test at all, extending the module-level
    annotate() and note_exception() contract of being safe to call
    unconditionally.
    """

    __slots__ = ("_event", "_kind", "_of")

    def __init__(self, event: Event | None, kind: str | None, of: Any) -> None:
        object.__setattr__(self, "_event", event)
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_of", of)

    def _describe(self) -> str:
        # The current_event() call this handle came from, for messages.

        filters = []
        if self._kind is not None:
            filters.append(f"kind={self._kind!r}")
        if self._of is not None:
            filters.append(f"binding={self._of!r}")

        return f"current_event({', '.join(filters)})"

    def __getattr__(self, name: str) -> Any:
        event = self._event
        if event is None:
            raise AttributeError(
                f"no in-flight event matched {self._describe()}, so the"
                f" handle is empty and has no {name!r}; test the handle's"
                f" truthiness before reading fields"
            )

        return getattr(event, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"{type(self).__name__} is read-only: events change through"
            f" the annotate() and note_exception() verbs, or through"
            f" behaviours"
        )

    def __bool__(self) -> bool:
        return self._event is not None

    def __eq__(self, other: Any) -> bool:
        # Events compare by identity, so a handle compares as the event
        # it wraps: equal to another handle on the same event, and to
        # the event itself (Event's reflected comparison defers here).

        if isinstance(other, EventHandle):
            return self._event is other._event
        if isinstance(other, Event):
            return self._event is other

        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._event)

    def __repr__(self) -> str:
        if self._event is None:
            return f"<EventHandle empty: {self._describe()} matched nothing>"

        return f"<EventHandle {self._event!s}>"

    def __str__(self) -> str:
        if self._event is None:
            return repr(self)

        return str(self._event)

    def annotate(self, **data: Any) -> None:
        """Merge values into the event's data dict.

        Annotation is targeted capture: the caller attaches what it
        knows a generic policy cannot infer (a row count, a cache hit,
        an immutable copy of a value that will be mutated). A silent
        no-op on an empty handle, so it is safe to call
        unconditionally; refused with a ConfigWarning on an event that
        has already finished, since the sinks have already heard that
        event close. The module-level annotate(**data) is the shortcut
        for current_event().annotate(**data).
        """

        event = self._event
        if event is None:
            return

        if event.finished:
            warnings.warn(
                f"annotate() ignored for {event}: the event has already"
                f" finished, so no sink can be told of the new data;"
                f" annotate an event still in flight",
                ConfigWarning,
                stacklevel=2,
            )
            return

        event.data.update(data)

    def note_exception(self, exception: BaseException) -> None:
        """Note a caught exception against the event, without changing
        control flow.

        For the code that handles an exception rather than letting it
        escape: a framework's error handler is passed the exception and
        returns a normal response, so the scope that failed completes
        with a result and wrapture would otherwise never know. The note
        records the exception on the event's `caught` sequence with the
        moment it was noted, marks the event as failed for every sink,
        filter and renderer, and leaves the exception's own fate to the
        caller.

        A silent no-op on an empty handle, so aiming with filters is
        safe to do unconditionally; the module-level note_exception(exc)
        is the shortcut for current_event().note_exception(exc).

        Noting the same exception object twice against one event records
        it once, and an exception noted against an event that it later
        escapes shows once, as the escape. A note against an event that
        has already finished is refused with a ConfigWarning and leaves
        the event unchanged: the sinks have already heard that event
        close, so nothing could carry the note to them, and the failure
        needs recording another way.
        """

        event = self._event
        if event is None:
            return

        _note_exception(event, exception, stacklevel=3)


def current_event(kind: str | None = None, binding: Any = None) -> EventHandle:
    """A handle on the in-flight event, empty when nothing matched.

    The behaviour pipeline runs after its event is pushed, so this is
    reachable from inside a decorates() handler, where it names the
    event for the very call the handler is wrapping.

    The filters aim further out, at the nearest enclosing event of a
    given identity: `kind=` selects by what sort of thing the event is
    ("request" for the request wrapture's own middleware recorded),
    `binding=` by which binding recorded it (the handle an
    instrumentation holds on its own binding, or a behaviour namespace
    standing in for it). Either walks the in-flight stack from the
    innermost event outward and takes the first match; given both,
    both must match. This is how a handler that was passed an
    exception reaches the unit of work that actually failed:

        current_event(kind="request").note_exception(exc)

    The result is always an EventHandle, never None: empty and falsy
    when no event matched, with verbs that then do nothing, so aimed
    annotate() and note_exception() calls need no guard.
    """

    stack = _stack.get()

    found: Event | None = None

    if kind is None and binding is None:
        found = stack[-1] if stack else None
    else:
        resolved = getattr(binding, "_binding", binding)

        for event in reversed(stack):
            if kind is not None and event.kind != kind:
                continue
            if resolved is not None and event.binding is not resolved:
                continue
            found = event
            break

    return EventHandle(found, kind, binding)


def current_trace() -> _trace.TraceContext | None:
    """The distributed trace identity of the in-flight event's tree,
    or None when nothing is being recorded or the tree carries none.

    This is the read half of the public surface instrumentation
    packages build on: what trace is this operation part of. Treat
    the returned context as read only; the write side belongs to
    tracing sinks, which are internals territory.
    """

    stack = _stack.get()
    return stack[-1].trace if stack else None


def trace_headers() -> dict[str, str]:
    """The name-value pairs an outbound message sent now should carry,
    so the service it reaches joins this tree's distributed trace.

    The injection convenience for any carrier of named values: HTTP
    request headers foremost, and equally message-queue headers or
    gRPC metadata; a probe calls this and merges the result into
    whatever it is sending. Claimed and minted identities render from
    their current ids; an identity that arrived in headers and was
    never claimed forwards verbatim, so a product wrapture has no
    sink for sees a transparent hop rather than a broken trace. Empty
    when
    nothing is being recorded or the tree carries no identity, so
    injection is always safe to attempt.

    A carrier with no header concept (trace context in a SQL comment,
    say) reads current_trace() instead and renders the slot's ids its
    own way, forgoing the verbatim pass-through only headers can
    honour.
    """

    context = current_trace()
    if context is None:
        return {}

    return _trace.headers_for(context)


def annotate(**data: Any) -> None:
    """Merge values into the in-flight event's data dict.

    The shortcut for current_event().annotate(**data): annotation is
    targeted capture, the caller attaching what it knows a generic
    policy cannot infer (a row count, a cache hit, an immutable copy
    of a value that will be mutated). A silent no-op when nothing is
    being recorded, so observed code can call it unconditionally; to
    aim at an enclosing event instead of the innermost one, go through
    current_event() with its filters.
    """

    current_event().annotate(**data)


def _note_exception(event: Event, exception: BaseException, stacklevel: int) -> None:
    # The shared body of the module-level and handle note_exception()
    # verbs, both of which sit one frame above the caller the warning
    # should point at.

    if event.finished:
        warnings.warn(
            f"note_exception() ignored for {event}: the event has already"
            f" finished, so no sink can be told of the"
            f" {type(exception).__name__}; note it against an event still in"
            " flight, or record the failure another way",
            ConfigWarning,
            stacklevel=stacklevel,
        )
        return

    if any(caught.exception is exception for caught in event.caught):
        return

    event.caught = event.caught + (CaughtException(exception, time.perf_counter()),)


def note_exception(exception: BaseException) -> None:
    """Note a caught exception against the in-flight event, without
    changing control flow.

    The shortcut for current_event().note_exception(exception), which
    from inside a bound handler notes against the handler's own call.
    To aim at the unit of work that actually failed, go through
    current_event() with its filters:

        current_event(kind="request").note_exception(exc)

    for a request wrapture's middleware recorded, or
    current_event(binding=...) for an event of the caller's own
    binding. A silent no-op when nothing is being recorded, so
    instrumentation calls it unconditionally; the semantics of noting
    (dedupe, escape precedence, the refusal on finished events) are
    the handle verb's, documented there.
    """

    handle = current_event()

    event = handle._event
    if event is None:
        return

    _note_exception(event, exception, stacklevel=3)


class _BlockRecorder:
    """The identity a block event carries in its binding slot: one
    shared object per block name, so blocks of the same name read as
    one recorder to assert_order()'s strictness flags, the way calls
    of one binding do.

    Interned weakly and kept alive by the events that carry it, so a
    process using dynamically named blocks (`block(f"deploy
    {target}")`) accumulates no per-name registry entries beyond the
    lives of the events themselves.
    """

    __slots__ = ("label", "__weakref__")

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"<block {self.label!r}>"


_block_recorders: weakref.WeakValueDictionary[str, _BlockRecorder] = (
    weakref.WeakValueDictionary()
)
_block_recorders_lock = threading.Lock()


def _block_recorder(name: str) -> _BlockRecorder:
    with _block_recorders_lock:
        recorder = _block_recorders.get(name)

        if recorder is None:
            recorder = _BlockRecorder(name)
            _block_recorders[name] = recorder

        return recorder


class Block:
    """The context manager block() returns, recording one "block" event
    per use of the with statement.

    The recording work all happens on entry and exit; the object holds
    only what exit needs to close the event entry opened.
    """

    def __init__(self, name: str, data: dict[str, Any]) -> None:
        if not isinstance(name, str) or not name:
            raise TypeError(f"block() needs a non-empty name string, got {name!r}")

        self._name = name
        self._data = data

        self._event: Event | None = None
        self._token: contextvars.Token[tuple[Event, ...]] | None = None
        self._active: tuple[Sink, ...] = ()
        self._started = 0.0

    def __enter__(self) -> None:
        # Recording gate first: with nobody listening, or inside the
        # recording machinery itself, the marker does nothing at all,
        # not even the frame inspection.

        active = _active_sinks()
        if not active or _in_recorder.get():
            return None

        if self._event is not None:
            raise RuntimeError(
                "this block is already active; each with statement needs"
                " its own block()"
            )

        # Synthesise the path from the caller's frame, so the event
        # locates its call site the way a bound call would.

        frame = sys._getframe(1)
        module = frame.f_globals.get("__name__", "?")
        path = f"{module}:{frame.f_code.co_qualname}"

        event = Event(
            "block", path, label=self._name, binding=_block_recorder(self._name)
        )
        if self._data:
            event.data.update(self._data)

        # Push before delivery, as every producer does: the event is
        # the innermost in-flight event from the moment sinks hear of
        # it, so everything recorded inside the body nests under it.

        self._token = _push(event)
        self._active = active
        self._event = event

        _record_event(event, active)

        self._started = time.perf_counter()
        event.started = self._started

        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        event = self._event
        if event is None:
            return

        # Close with duration and outcome, then restore the enclosing
        # scope. An exception is recorded and still propagates.

        self._event = None
        event.duration = time.perf_counter() - self._started

        try:
            if exc is not None:
                event.exception = exc
                _notify_error(event, self._active)
            else:
                _notify_exit(event, self._active)
        finally:
            token = self._token
            self._token = None
            if token is not None:
                _pop(token)


_SCALARS = (str, int, float, bool)


def seed_data(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and copy declaration-point seed data.

    Accepts None (nothing) or a mapping of non-empty string keys to
    scalars (str, int, float, bool) or flat lists of scalars: the shape
    a TOML table and an OTel attribute both handle without coercion.
    Anything else raises TypeError naming the offending key. Used by
    binding(), block() and the config loader's observe entries, so the
    three declaration points accept exactly one shape.
    """

    if data is None:
        return {}

    if not isinstance(data, Mapping):
        raise TypeError(f"data must be a mapping of string keys, got {data!r}")

    seeded: dict[str, Any] = {}

    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise TypeError(f"data keys must be non-empty strings, got {key!r}")

        if isinstance(value, _SCALARS):
            seeded[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, _SCALARS) for item in value
        ):
            seeded[key] = list(value)
        else:
            raise TypeError(
                f"data[{key!r}] must be a str, int, float or bool, or a flat"
                f" list of those, got {value!r}"
            )

    return seeded


def block(name: str, *, data: Mapping[str, Any] | None = None) -> Block:
    """Declare the enclosed stretch of code as one recorded event.

    A named unit smaller than a function: the with body's wall time
    becomes the event's duration, an exception escaping the body is
    recorded and still propagates, `data=` seeds the event's data (a
    mapping of string keys to scalars or flat lists of scalars, the
    same shape `binding()` and an observe entry take; anything known
    only inside the body is `annotate()`'s job), and everything
    recorded inside the body (bound calls, log events, nested blocks)
    nests under it. The event's kind is
    "block", its label is the given name, and its path locates the
    call site as module:qualname of the function the with statement
    sits in. A block entered with nothing in flight above it roots
    its own tree and mints a trace identity like any operation root.

    Like a log statement, a marker left permanently in code is inert
    when nothing is listening: with no sinks active, nothing is built
    at all. The context manager yields None; code inside the block
    reaches the event through the ambient surface, annotate() and
    current_event().
    """

    return Block(name, seed_data(data))


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
