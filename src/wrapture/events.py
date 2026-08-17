"""The event record produced when a binding observes something happen.

One record type covers all four kinds of observation: a call to a wrapped
callable, and a read, write or delete of a wrapped attribute. The kinds
share the fields that describe where and when the event happened and how
events nest; each kind then populates the fields that make sense for it.

Nothing in this module records anything by itself. Events are created by
the recording machinery when a binding fires inside a timeline, and are
consumed through the event log and tape interfaces built on top of them.
"""

from __future__ import annotations

import inspect
import threading
import weakref
from dataclasses import dataclass, field
from typing import Any, Literal

from wrapt import MISSING

from .capture import REFERENCE

EventKind = Literal["call", "get", "set", "delete", "request"]


@dataclass(eq=False)
class Event:
    """One recorded occurrence at a binding.

    The `kind` field says what happened: "call" for an invocation of a
    wrapped callable, "get", "set" or "delete" for attribute access, and
    "request" for an HTTP request through a wrapped WSGI application.
    Fields that were not observed hold the MISSING sentinel (for values,
    so that a recorded None stays distinguishable) or None (for the
    optional descriptive fields). Events compare by identity: two events
    with identical fields are still two distinct occurrences.
    """

    kind: EventKind

    # Where it happened: path is the fully qualified location of what
    # was bound, in module:path form with both halves dotted, so it
    # stays meaningful after events leave the process. label is the
    # friendly display name, from the binding's label.

    path: str
    label: str | None = None
    instance: Any = None

    # The Binding that recorded this event. Typed loosely because the
    # bindings module builds on this one; excluded from repr() because
    # the path already identifies the event.

    binding: Any = field(default=None, repr=False)

    # Position on the tape: allocation order, nesting depth, and the
    # sequence number of the enclosing event. The link is an id rather
    # than an object reference, so an event can be serialised and leave
    # the process without dragging its tree along, and so a retained
    # event does not keep its ancestors alive. The tape resolves the id
    # back to an event with parent_of() and children_of().

    seq: int = 0
    depth: int = 0
    parent_id: int | None = None

    started: float | None = None
    duration: float | None = None

    # The thread the operation began on, captured when the event is
    # constructed. A generator or coroutine may run and complete on
    # other threads; the identity recorded here is where it began.

    thread_id: int = field(default_factory=threading.get_ident, repr=False)
    thread_name: str = field(
        default_factory=lambda: threading.current_thread().name, repr=False
    )

    # Iteration, for a call that produced a generator, or a request
    # streaming its body: how many items (or body chunks) it has yielded
    # so far, and the accumulated time its body ran across resumptions.
    # duration is then wall time from creation to close, which includes
    # all the consumer's time between yields, so the two answer
    # different questions.

    items: int | None = None
    body_duration: float | None = None

    # Outcome. For a call this is the return value or the exception it
    # raised; for a get it is the value read, so the same accessors and
    # filters work across calls and reads.

    result: Any = MISSING
    exception: BaseException | None = None

    # kind == "call": the arguments as sent, the signature-normalized
    # form with defaults applied, and the (args, kwargs) actually passed
    # on when behaviour transformed them.

    args: tuple[Any, ...] | None = None
    kwargs: dict[str, Any] | None = None
    arguments: dict[str, Any] | None = None
    forwarded: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    # kind == "set": the value written, and the prior value where it was
    # cheaply available.

    value: Any = MISSING
    previous: Any = MISSING

    # Recording provenance: the capture level values were recorded at,
    # whether the outcome was supplied by returns()/raises() rather than
    # produced by the real operation, the interned id of the captured
    # call stack when the binding asked for one, and caller-supplied
    # annotations merged in with annotate().

    capture: int = REFERENCE
    injected: bool = False
    stack: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        # Display favours the friendly label; the path is there when no
        # label was recorded, and for anything that needs the location
        # rather than the name.

        where = self.label or self.path

        if self.kind == "call":
            return f"{where}({self._format_arguments()})"

        # A request reads access-log style: method and path first, the
        # bound location in parentheses.

        if self.kind == "request":
            method = self.data.get("method", "?")
            target = self.data.get("path", "")
            query = self.data.get("query")

            if query:
                target = f"{target}?{query}"
            return f"{method} {target} ({where})"

        if self.kind == "get":
            if self.result is not MISSING:
                return f"get {where} -> {self.result!r}"
            return f"get {where}"

        if self.kind == "set":
            return f"set {where} = {self.value!r}"

        return f"delete {where}"

    def _format_arguments(self) -> str:
        # Prefer the normalized form so the display matches what filters
        # and assertions compare against; fall back to the raw call shape
        # when no signature was available.

        if self.arguments is not None:
            return ", ".join(f"{k}={v!r}" for k, v in self.arguments.items())

        positional = [repr(a) for a in (self.args or ())]
        keyword = [f"{k}={v!r}" for k, v in (self.kwargs or {}).items()]
        return ", ".join(positional + keyword)


def _own_time(event: Event) -> float | None:
    # The execution-time basis for self-time arithmetic. A generator's
    # wall duration includes the consumer's time between yields, so its
    # accumulated body time is the honest measure of the code itself;
    # everything else uses its duration. None when the event has not
    # closed with a time.

    if event.body_duration is not None:
        return event.body_duration
    return event.duration


# Signature lookup is cached because inspect.signature costs microseconds
# per call and would dominate the recording path. The cache is keyed on
# the function itself via weak references, never on id(): ids are reused
# after garbage collection, and a collision would silently bind one
# function's arguments against another's signature.

_signature_cache: weakref.WeakKeyDictionary[Any, inspect.Signature | None] = (
    weakref.WeakKeyDictionary()
)


def _signature(func: Any) -> inspect.Signature | None:
    try:
        return _signature_cache[func]
    except KeyError:
        pass
    except TypeError:
        # Unhashable or not weak-referenceable: compute every time.

        try:
            return inspect.signature(func)
        except (TypeError, ValueError):
            return None

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        signature = None

    try:
        _signature_cache[func] = signature
    except TypeError:
        pass

    return signature


def normalized_arguments(
    func: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any] | None:
    """Bind args and kwargs against func's signature, defaults applied.

    Returns a dict mapping parameter names to values, so that f(1, 2)
    and f(1, b=2) produce the same recorded arguments. Returns None when
    no signature is available or the arguments do not fit it, in which
    case the caller falls back to the raw call shape.
    """

    signature = _signature(func)
    if signature is None:
        return None

    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError:
        return None

    bound.apply_defaults()
    return dict(bound.arguments)
