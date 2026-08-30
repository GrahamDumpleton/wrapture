"""The event record produced when a binding observes something happen.

One record type covers every kind of observation: a call to a wrapped
callable, a read, write or delete of a wrapped attribute, an HTTP
request through wrapped middleware, a log message the observed code
emitted, and a block of code the observed code declared. The kinds
share the fields that describe where and when the event happened and
how events nest; each kind then populates the fields that make sense
for it.

Nothing in this module records anything by itself. Events are created by
the recording machinery when a binding fires inside a timeline, and are
consumed through the event log and tape interfaces built on top of them.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

from wrapt import MISSING

from .capture import REFERENCE
from .trace import TraceContext

EventKind = Literal["call", "get", "set", "delete", "request", "log", "block"]


@dataclass(frozen=True)
class EventLink:
    """A causal reference from a root event to one that caused it but
    does not contain it: the origin of work handed to another thread
    or, through a message, to another process.

    `trace_id` and `span_id` are the origin's W3C identity as it stood
    at the hand-off, lowercase hex, or None when the origin's tree
    carried no trace identity; `seq` is the origin's sequence number
    when it was recorded in this process, None for an origin in
    another process. `attributes` are facts about the hand-off known
    where the link was made (a message id, a queue name), scalars or
    flat lists of scalars. Containment stays on `parent_id`; a link
    says only "that operation started this one".
    """

    trace_id: str | None = None
    span_id: str | None = None
    seq: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaughtException:
    """One exception noted against an event with note_exception().

    `exception` is the exception object as handed over, its traceback
    riding along; `at` is the moment of the note on the perf_counter
    clock, the same clock as the event's `started`, so a sink can
    place it in time.
    """

    exception: BaseException
    at: float


@dataclass(eq=False)
class Event:
    """One recorded occurrence at a binding.

    The `kind` field says what happened: "call" for an invocation of a
    wrapped callable, "get", "set" or "delete" for attribute access,
    "request" for an HTTP request through a wrapped WSGI application,
    "log" for a log message the observed code emitted, and "block" for
    a stretch of code the observed code declared with block().
    Fields that were not observed hold the MISSING sentinel (for values,
    so that a recorded None stays distinguishable) or None (for the
    optional descriptive fields). Events compare by identity: two events
    with identical fields are still two distinct occurrences.
    """

    kind: EventKind

    # Where it happened: path is the fully qualified location of what
    # was bound, in module:path form with both halves dotted, so it
    # stays meaningful after events leave the process. label is an
    # assigned display name, or None when none was given, in which
    # case every consumer falls back to the path: a name with a colon
    # in it is always the real module:qualname location, a name
    # without one is a name somebody chose.

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

    # Causal origins that are not the parent: the operations this
    # event's tree was handed off from, on another thread or from
    # another process. Only ever set on a root; a child carries
    # nothing, since its parent already holds the link.

    links: tuple[EventLink, ...] = ()

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
    # filters work across calls and reads. `exception` is the one that
    # escaped the scope; `caught` holds the exceptions the observed code
    # handled itself and reported with note_exception(), in the order
    # noted. The two are distinct facts: a scope can return normally
    # and still carry a failure. Replaced, never mutated, on each note,
    # so a reader never sees a half-built sequence.

    result: Any = MISSING
    exception: BaseException | None = None
    caught: tuple[CaughtException, ...] = ()

    # kind == "call": the arguments as sent, the signature-normalized
    # form with defaults applied, and the (args, kwargs) actually passed
    # on when behaviour transformed them.

    args: tuple[Any, ...] | None = None
    kwargs: dict[str, Any] | None = None
    arguments: dict[str, Any] | None = None
    forwarded: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    # The name of the signature's var-keyword parameter (**kwargs), when
    # the target has one and the arguments were normalized: the entry in
    # `arguments` under this name is the bundle of extra keywords the
    # call passed, and with_args() falls through into it for names that
    # are not parameters. None for attribute events and for calls with
    # no readable signature.

    var_keyword: str | None = None

    # kind == "set": the value written, and the prior value where it was
    # cheaply available.

    value: Any = MISSING
    previous: Any = MISSING

    # Recording provenance: the capture level values were recorded at,
    # whether the outcome was supplied by returns()/raises() rather than
    # produced by the real operation, which phase of a phased binding
    # handled the operation (None when the binding has a single phase),
    # the interned id of the captured call stack when the binding asked
    # for one, and caller-supplied annotations merged in with annotate().

    capture: int = REFERENCE
    injected: bool = False
    phase: int | None = None
    stack: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    # The distributed trace identity this event's tree carries, shared
    # by reference with every event in the tree: parsed from incoming
    # headers at a request boundary, minted at a root otherwise, None
    # when the trace mechanism is disabled. The identity fields are
    # written once, before any consumer can read them; only a tracing
    # sink's span-id register changes afterwards.

    trace: TraceContext | None = field(default=None, repr=False)

    @property
    def finished(self) -> bool:
        """Whether the operation has ended, however it ended: returned,
        raised, or, for a generator, closed. False while a call is in
        flight, a generator is still being consumed, or a coroutine has
        been created but not yet awaited."""

        return self.duration is not None

    @property
    def failed(self) -> bool:
        """Whether the operation failed, however the failure surfaced:
        an exception escaped the scope, or one was caught inside it and
        noted against the event with note_exception()."""

        return self.exception is not None or bool(self.caught)

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

        # A log message reads as one line: the logger, the level, and
        # the message repr-escaped so an embedded newline can never
        # break a tree's alignment.

        if self.kind == "log":
            level = self.data.get("level", "?")
            message = self.data.get("message", "")
            return f"log {where} {level} {message!r}"

        # A block's name is free-form and may contain spaces, so the
        # colon delimits it from the kind word.

        if self.kind == "block":
            return f"block: {where}"

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


def _format_links(event: Event, resolve: Callable[[int], Event | None]) -> str:
    # The `<- origin` marker a root with links carries in tree() and
    # the Printer sink: the origin's own display name when `resolve`
    # finds it on the same tape, else its trace id, else its sequence
    # number, so the marker always says something. Empty for an event
    # with no links.

    if not event.links:
        return ""

    parts: list[str] = []

    for link in event.links:
        origin = resolve(link.seq) if link.seq is not None else None

        if origin is not None:
            parts.append(origin.label or origin.path)
        elif link.trace_id is not None:
            parts.append(f"trace {link.trace_id}")
        else:
            parts.append(f"#{link.seq}")

    return "  <- " + ", ".join(parts)


def _caught_types(event: Event) -> list[str]:
    # The type names of the exceptions noted against the event, in the
    # order noted, for the renderers that mark each with `!!` after
    # the outcome: tree(), the Printer sink and the canonical export
    # all draw from this so they agree.

    return [type(caught.exception).__name__ for caught in event.caught]


def _format_time(seconds: float) -> str:
    # Adaptive units so a display of mixed magnitudes stays readable:
    # seconds, milliseconds, then microseconds. Shared by tape.tree()
    # and the Printer sink so the two views agree.

    if seconds >= 1.0:
        return f"{seconds:.2f}s"
    if seconds >= 0.001:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds * 1_000_000:.0f}us"


def _own_time(event: Event) -> float | None:
    # The execution-time basis for self-time arithmetic. A generator's
    # wall duration includes the consumer's time between yields, so its
    # accumulated body time is the honest measure of the code itself. A
    # request has two phases of application code, the synchronous call
    # and the body it then streams, and its own time is their sum, again
    # leaving out the server's time between chunks. Everything else uses
    # its duration. None when the event has not closed with a time.

    if event.kind == "request" and event.body_duration is not None:
        app_duration = event.data.get("app_duration")
        if app_duration is None:
            return event.duration
        return float(app_duration) + event.body_duration

    if event.body_duration is not None:
        return event.body_duration
    return event.duration


# Signature lookup is cached by the owner of a call site (a Binding or
# an ObservedCallable) rather than by callable, because inspect.signature
# costs microseconds and would dominate the recording path. The cache is
# keyed by the type wrapt presented the target as: a bound method, the
# partial proxy wrapt builds for a method called through its class, or a
# plain function. The signature differs between those forms (a bound
# method's drops self) but is fixed for each, so one entry per form seen
# is exact, and the owner's lifetime is the cache's lifetime.


class ParameterTable(NamedTuple):
    """A flattened view of a signature made only of positional and
    positional-or-keyword parameters: their names in order, how many
    leading ones are positional-only, and the defaults by name. The
    common case, which binds by walking the names without going
    through `Signature.bind`."""

    names: tuple[str, ...]
    positional_only: int
    defaults: dict[str, Any]


class SignatureInfo(NamedTuple):
    """A callable's signature as inspect reports it, None when it has no
    readable one, with the name of its var-keyword parameter (**kwargs)
    resolved once alongside, and the parameter table when the signature
    is simple enough to bind without `Signature.bind`."""

    signature: inspect.Signature | None
    var_keyword: str | None
    table: ParameterTable | None = None


_SIMPLE_KINDS = frozenset(
    {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
)


def signature_info(func: Any) -> SignatureInfo:
    """Resolve the SignatureInfo for func."""

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return SignatureInfo(None, None)

    var_keyword = None
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            var_keyword = parameter.name

    return SignatureInfo(signature, var_keyword, _parameter_table(signature))


def _parameter_table(signature: inspect.Signature) -> ParameterTable | None:
    # Only a signature with nothing but positional and
    # positional-or-keyword parameters gets a table; *args, **kwargs
    # and keyword-only parameters are left to Signature.bind.

    parameters = signature.parameters.values()
    if any(parameter.kind not in _SIMPLE_KINDS for parameter in parameters):
        return None

    names = tuple(parameter.name for parameter in parameters)
    positional_only = sum(
        parameter.kind is inspect.Parameter.POSITIONAL_ONLY for parameter in parameters
    )
    defaults = {
        parameter.name: parameter.default
        for parameter in parameters
        if parameter.default is not inspect.Parameter.empty
    }

    return ParameterTable(names, positional_only, defaults)


def cached_signature_info(
    cache: dict[type, SignatureInfo], wrapped: Any
) -> SignatureInfo:
    """The SignatureInfo for wrapped from cache, keyed by the type wrapt
    presented it as, resolved and stored on first sight."""

    key = type(wrapped)

    try:
        return cache[key]
    except KeyError:
        info = cache[key] = signature_info(wrapped)
        return info


def normalized_arguments(
    signature: inspect.Signature | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    table: ParameterTable | None = None,
) -> dict[str, Any] | None:
    """Bind args and kwargs against a signature, defaults applied.

    Returns a dict mapping parameter names to values, so that f(1, 2)
    and f(1, b=2) produce the same recorded arguments. Returns None when
    no signature is available or the arguments do not fit it, in which
    case the caller falls back to the raw call shape.

    With `table` given, the signature's parameter table, the common
    shapes bind by walking the names, which costs a fraction of
    `Signature.bind`; anything the walk cannot settle exactly falls
    back to `Signature.bind`, so the result is the same either way.
    """

    if signature is None:
        return None

    if table is not None:
        arguments = _bind_by_table(table, args, kwargs)
        if arguments is not None:
            return arguments

    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError:
        return None

    bound.apply_defaults()
    return dict(bound.arguments)


def _bind_by_table(
    table: ParameterTable, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any] | None:
    # Walk the parameters in order, taking each from the positionals,
    # then the keywords, then the defaults, which is the order
    # Signature.bind produces. None means "not settled here": too many
    # positionals, a keyword naming a positional-only or an
    # already-bound parameter, an unknown keyword, or a parameter with
    # nothing to fill it. Those are Signature.bind's to judge, and it
    # reports the same None for the ones that really do not fit.

    names = table.names
    given = len(args)
    if given > len(names):
        return None

    arguments = dict(zip(names, args, strict=False))

    if kwargs:
        defaults = table.defaults
        consumed = 0
        for index in range(given, len(names)):
            name = names[index]
            if name in kwargs:
                if index < table.positional_only:
                    return None
                arguments[name] = kwargs[name]
                consumed += 1
            elif name in defaults:
                arguments[name] = defaults[name]
            else:
                return None

        if consumed != len(kwargs):
            return None
    elif given < len(names):
        defaults = table.defaults
        for name in names[given:]:
            if name not in defaults:
                return None
            arguments[name] = defaults[name]

    return arguments
