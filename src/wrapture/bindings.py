"""Bindings: the association of a target attribute with a wrapper.

A binding is created with binding() and names one attribute of a module,
class or instance. Applying it installs a wrapt wrapper on the target;
behaviour configured through the binding's namespaces then applies to every
call until the binding is removed, suspended or reconfigured.
"""

from __future__ import annotations

import importlib
import inspect
import time
import types
import warnings
import weakref
from collections.abc import AsyncGenerator, Callable, Generator, Iterable, Sequence
from fnmatch import fnmatchcase
from typing import Any, Protocol, Self, TypeVar, cast

import wrapt
from wrapt import MISSING, is_wrapped_by, unwrap_object

from .attributes import install as install_attribute
from .behaviours import (
    CallBehaviour,
    DeleteBehaviour,
    GetBehaviour,
    Phase,
    SetBehaviour,
    StageFunction,
    WrappedFunction,
    WrapperFunction,
    _Behaviour,
)
from .capture import (
    NONE,
    REFERENCE,
    CapturePolicy,
    _capture_value,
    _level_of,
    _resolve_policy,
)
from .eventlogs import EventLog
from .events import Event, normalized_arguments
from .exceptions import (
    AlreadyAppliedError,
    DeferredTargetError,
    ExpectationNotMetError,
    NeverAppliedError,
    RecordingGapWarning,
    WrongModeError,
)
from .sinks import (
    Sink,
    _active_sinks,
    _in_recorder,
    _notify_error,
    _notify_exit,
    _record_event,
    _required_policy,
)
from .stacks import _capture as _capture_stack
from .stacks import _resolve_depth
from .timeline import (
    _capture_result,
    _current_tape,
    _pop,
    _push,
    _stack,
    _timelines_active,
)


class RequestNamespace(Protocol):
    """The static face of on_request: the combined WSGI and ASGI
    vocabulary, permissively typed.

    Which half is live depends on the binding's mode, which no type
    checker can see, so the signatures here accept anything plausible
    and the accurate per-protocol types are documented on the concrete
    classes, wrapture.wsgi.RequestBehaviour and
    wrapture.asgi.RequestBehaviour. Calling a stage from the wrong
    protocol fails at runtime with an AttributeError.
    """

    def transforms_environ(
        self, fn: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> Binding: ...

    def transforms_scope(
        self, fn: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> Binding: ...

    def transforms_response(
        self, fn: Callable[[Any, Any], tuple[Any, Any]]
    ) -> Binding: ...

    def transforms_body(self, fn: Callable[[Any], Any]) -> Binding: ...

    def returns(
        self,
        status: Any,
        headers: Iterable[tuple[str, str]] = (),
        body: Any = (),
    ) -> Binding: ...

    def raises(self, exc: BaseException | type[BaseException]) -> Binding: ...

    def decorates(self, fn: Callable[..., Any]) -> Binding: ...

    def passes_through(self) -> Binding: ...


_BehaviourT = TypeVar("_BehaviourT", bound=_Behaviour)

# Every currently applied binding, so tooling such as the pytest plugin
# can sweep for patches left behind. Weak references, so the registry
# never keeps a binding alive.

_applied_bindings: weakref.WeakSet[Binding] = weakref.WeakSet()


def _reject_deferred(target: Any) -> None:
    """Reject wrapt's `?` deferred-patching syntax.

    Only a trailing `?` on a string target is rejected; that is the only
    position where wrapt gives it meaning. A `?` in `name` is an ordinary
    character that fails to resolve like any other typo.
    """

    if isinstance(target, str) and target.endswith("?"):
        raise DeferredTargetError(
            f"deferred patching is not supported: target {target!r} uses"
            f" wrapt's trailing `?` syntax. A deferred wrap registers a"
            f" post-import hook and returns no handle, so a binding would"
            f" have nothing to remove, suspend or query. Import the module"
            f" first and bind against it, or create the binding inside a"
            f" `wrapture.when_imported` hook for that module."
        )


def _detect_mode(target: Any, name: str, missing_ok: bool = False) -> str:
    """Decide whether this binding wraps a call or an attribute access.

    Classified from whatever `resolve_path` finds at the target:

        function / lambda / staticmethod / classmethod  -> "callable"
        property / member_descriptor / plain data       -> "attribute"
        other callable stored as data                   -> "callable"
        absent from the class                           -> error unless
                                                           missing_ok=True

    A callable stored as data is ambiguous; override with mode= if the
    guess is wrong. An absent attribute raises rather than being inferred,
    because it is indistinguishable from a typo.
    """

    try:
        value = wrapt.resolve_path(target, name)[2]
    except Exception:
        if missing_ok:
            return "attribute"
        raise

    # Classify what was found. The routine checks must come before the
    # descriptor check: functions are themselves descriptors, so testing
    # for __get__ first would classify every method as an attribute.

    if isinstance(value, staticmethod | classmethod):
        return "callable"
    if inspect.isroutine(value):
        return "callable"

    if hasattr(type(value), "__get__"):
        return "attribute"

    if callable(value):
        return "callable"

    return "attribute"


def _select_members(
    container: Any, match: Sequence[str], exclude: Sequence[str]
) -> list[str]:
    """Select the members of a container that match a pattern.

    Pattern selection is deliberately confined: immediate members from
    the container's own vars() only, never inherited and never
    traversing into nested classes or submodules, matched with
    fnmatchcase against the bare names. Only routines are eligible;
    properties, other descriptors, nested classes and plain data are
    skipped, as is anything already wrapped, and a module's imported
    functions and classes are skipped so a module pattern selects only
    what the module itself defines. Binding by exact name is the escape
    hatch for any of the skipped kinds. Shared by discover() and the
    config layer's match entries, so a pattern selects the same members
    however it is spelt.
    """

    is_module = inspect.ismodule(container)

    selected: list[str] = []
    for member, value in vars(container).items():
        if not any(fnmatchcase(member, pattern) for pattern in match):
            continue
        if any(fnmatchcase(member, pattern) for pattern in exclude):
            continue

        if isinstance(value, wrapt.BaseObjectProxy):
            continue

        if is_module:
            eligible = (
                inspect.isroutine(value)
                and getattr(value, "__module__", None) == container.__name__
            )
        else:
            eligible = isinstance(
                value, (staticmethod, classmethod)
            ) or inspect.isfunction(value)

        if eligible:
            selected.append(member)

    return selected


def _derive_path(target: Any, name: str) -> str:
    """The fully qualified location of the bound attribute.

    Format is module:path, both halves dotted, as used by setuptools
    entry points: everything before the colon is the module to import,
    everything after is the attribute path within it. Derived from the
    target, so the same attribute yields the same path however the
    target was expressed, and unaffected by any label override.
    """

    if isinstance(target, str):
        return f"{target}:{name}"

    if isinstance(target, types.ModuleType):
        return f"{target.__name__}:{name}"

    # An instance target is located via its type: the events it records
    # carry the instance itself for anything the path cannot say.

    owner = target if isinstance(target, type) else type(target)
    return f"{owner.__module__}:{owner.__qualname__}.{name}"


def _forwarder(wrapped: WrappedFunction, event: Event) -> WrappedFunction:
    """The `wrapped` handed to behaviour, recording what the original
    actually received, which may differ from what the caller sent."""

    def forward(*args: Any, **kwargs: Any) -> Any:
        event.forwarded = (args, kwargs)
        return wrapped(*args, **kwargs)

    return forward


async def _record_awaited(
    awaitable: Any,
    event: Event,
    stack: tuple[Event, ...],
    policy: CapturePolicy,
    active: tuple[Sink, ...],
) -> Any:
    """Record around the await, so the event reflects the real outcome.

    Re-establishes the in-progress stack for the duration, so calls made
    inside the coroutine body nest under this event. The stack is set
    raw rather than pushed, because the event was already linked to its
    parent when the call was recorded.
    """

    token = _stack.set(stack + (event,))
    try:
        result = await awaitable
    except BaseException as exc:
        if event.started is not None:
            event.duration = time.perf_counter() - event.started
        event.exception = exc
        _notify_error(event, active)
        raise
    finally:
        _stack.reset(token)

    if event.started is not None:
        event.duration = time.perf_counter() - event.started
    _capture_result(event, result, policy)
    _notify_exit(event, active)
    return result


def _close_iteration(
    event: Event,
    started: float,
    body: float,
    items: int,
    policy: CapturePolicy,
    active: tuple[Sink, ...],
    result: Any = MISSING,
    exception: BaseException | None = None,
) -> None:
    # Close a generator's event: durations and the final item count
    # always, then the outcome. An abandoned generator supplies neither
    # a result nor an exception, so its event closes with no outcome and
    # stays visibly unfinished on the tape; abandonment is still an
    # exit, not an error.

    event.duration = time.perf_counter() - started
    event.body_duration = body
    event.items = items

    if exception is not None:
        event.exception = exception
        _notify_error(event, active)
        return

    if result is not MISSING:
        _capture_result(event, result, policy)

    _notify_exit(event, active)


def _record_generator(
    generator: Generator[Any, Any, Any],
    event: Event,
    stack: tuple[Event, ...],
    policy: CapturePolicy,
    active: tuple[Sink, ...],
) -> Generator[Any, Any, Any]:
    """A generator around a generator, recording as it runs.

    One event, already on the tape, covers the whole iteration. The
    in-progress stack is re-established around each resumption only, so
    calls made inside the body nest under the event while the consumer's
    own work between yields does not. Preserves the full generator
    protocol: send() and throw() are forwarded, close() closes the
    wrapped generator, and the return value is returned.
    """

    started = time.perf_counter()
    body = 0.0
    items = 0
    event.items = 0

    operation: tuple[str, Any] = ("send", None)

    while True:
        # Drive the wrapped generator with whatever the consumer last
        # did, timing the resumption: the body only runs inside send()
        # and throw().

        token = _stack.set(stack + (event,))
        resumed = time.perf_counter()

        try:
            if operation[0] == "send":
                item = generator.send(operation[1])
            else:
                item = generator.throw(operation[1])
        except StopIteration as stop:
            body += time.perf_counter() - resumed
            _stack.reset(token)
            _close_iteration(
                event, started, body, items, policy, active, result=stop.value
            )
            return stop.value
        except BaseException as exc:
            body += time.perf_counter() - resumed
            _stack.reset(token)
            _close_iteration(event, started, body, items, policy, active, exception=exc)
            raise
        else:
            body += time.perf_counter() - resumed
            _stack.reset(token)

        items += 1
        event.items = items

        try:
            operation = ("send", (yield item))
        except GeneratorExit:
            generator.close()
            _close_iteration(event, started, body, items, policy, active)
            raise
        except BaseException as exc:
            operation = ("throw", exc)


async def _record_async_generator(
    generator: AsyncGenerator[Any, Any],
    event: Event,
    stack: tuple[Event, ...],
    policy: CapturePolicy,
    active: tuple[Sink, ...],
) -> AsyncGenerator[Any, Any]:
    """The async twin of _record_generator, for async generators.

    Async generators have no return value, so exhaustion records a
    result of None, which is what keeps a finished iteration
    distinguishable from an abandoned one.
    """

    started = time.perf_counter()
    body = 0.0
    items = 0
    event.items = 0

    operation: tuple[str, Any] = ("send", None)

    while True:
        token = _stack.set(stack + (event,))
        resumed = time.perf_counter()

        try:
            if operation[0] == "send":
                item = await generator.asend(operation[1])
            else:
                item = await generator.athrow(operation[1])
        except StopAsyncIteration:
            body += time.perf_counter() - resumed
            _stack.reset(token)
            _close_iteration(event, started, body, items, policy, active, result=None)
            return
        except BaseException as exc:
            body += time.perf_counter() - resumed
            _stack.reset(token)
            _close_iteration(event, started, body, items, policy, active, exception=exc)
            raise
        else:
            body += time.perf_counter() - resumed
            _stack.reset(token)

        items += 1
        event.items = items

        try:
            operation = ("send", (yield item))
        except GeneratorExit:
            await generator.aclose()
            _close_iteration(event, started, body, items, policy, active)
            raise
        except BaseException as exc:
            operation = ("throw", exc)


class Binding:
    """The association of a target attribute with a wrapper and behaviour.

    Created by binding(); the wrapper is installed by apply() or by
    entering the binding as a context manager. Mixing the two lifecycle
    styles is an error.

    Two independent axes:

      apply() / remove()     whether the wrapper is applied to the target
      suspend() / resume()   whether an applied wrapper does anything

    Behaviour can be configured or reconfigured at any time, before or
    after apply().
    """

    def __init__(
        self,
        target: Any,
        name: str,
        *,
        label: str | None = None,
        mode: str | None = None,
        missing_ok: bool = False,
        capture: CapturePolicy | str | None = None,
        capture_args: CapturePolicy | str | None = None,
        capture_result: CapturePolicy | str | None = None,
        stack: int | str | None = None,
        when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any]
        | bool
        | None = None,
    ) -> None:
        # Validate the target and settle the mode before anything is
        # stored, so a bad binding fails on the line that created it.

        _reject_deferred(target)

        # A string target may spell the member's owner with the colon
        # convention ("module:path"), as observe targets, discover()
        # and event paths do. Fold the path half into the name, so the
        # colon spelling and the dotted-name spelling of the same
        # member are one binding from here on.

        if isinstance(target, str) and ":" in target:
            module_name, _, path = target.partition(":")

            target = module_name
            if path:
                name = f"{path}.{name}"

        stack = _resolve_depth(stack)
        if stack is not None and stack < 1:
            raise ValueError(
                f"stack must be None, 'caller', 'full' or a positive"
                f" frame count, got {stack!r}"
            )

        # As with wrapt's `enabled`, when= accepts a boolean as well as
        # a predicate: True is the always-record default, False makes
        # this a behaviour-only binding that never records.

        if when is True:
            when = None
        elif when is not False and when is not None and not callable(when):
            raise ValueError(
                f"when must be a boolean, a callable taking (instance,"
                f" args, kwargs), or None, got {when!r}"
            )

        if mode is None:
            mode = _detect_mode(target, name, missing_ok=missing_ok)
        elif mode not in ("callable", "attribute", "wsgi", "asgi"):
            raise ValueError(
                f"mode must be 'callable', 'attribute', 'wsgi' or 'asgi', got {mode!r}"
            )

        # What this binding is bound to.

        self._mode = mode
        self._target = target
        self._name = name
        self._path = _derive_path(target, name)
        self._label = label or self._default_label(target, name)
        self._missing_ok = missing_ok

        # Capture policy overrides. None means follow whatever the sink
        # consuming the events declares; capture= is shorthand for both
        # axes, with the specific parameters winning.

        self._capture_args = _resolve_policy(
            capture_args if capture_args is not None else capture
        )
        self._capture_result = _resolve_policy(
            capture_result if capture_result is not None else capture
        )
        self._stack_depth = stack
        self._when = when

        # The behaviour phases, keyed by operation ("call", "get", "set"
        # or "delete"): each operation has a chain of Phase records
        # starting at its head, and the phase currently deciding what
        # the operation does. Both are created on first use. Request
        # behaviour is stage-keyed rather than composed, so it lives in
        # its own structure, consumed by the WSGI and ASGI middlewares.

        self._heads: dict[str, Phase] = {}
        self._active: dict[str, Phase] = {}
        self._request_hooks: dict[str, Any] = {
            "inbound": [],
            "response": [],
            "body": [],
            "terminal": None,
        }

        # Lifecycle state, populated by apply() and cleared by remove().
        # The apply count survives remove(): it distinguishes a binding
        # that recorded nothing from one that was never applied at all.

        self._wrapper: Any = None
        self._suspended = False
        self._suspended_calls = 0
        self._apply_count = 0
        self._missed_calls = 0
        self._filtered_calls = 0
        self._gap_warned = False

        # Declared expectations, verified by the enclosing timeline at
        # exit. Like behaviour, they persist across apply/remove cycles.

        self._expectations: list[tuple[str, int]] = []

        # Whether request behaviour currently has an injecting terminal
        # (returns / rejects), so request events can be marked. The
        # composed operations carry the flag on their phases instead.

        self._injects: dict[str, bool] = {}

    @staticmethod
    def _default_label(target: Any, name: str) -> str:
        # A string target is already the module's name; anything else
        # is asked for its own name, with repr as the last resort.

        if isinstance(target, str):
            return f"{target}.{name}"

        owner = getattr(target, "__name__", None) or repr(target)
        return f"{owner}.{name}"

    # -- identity ----------------------------------------------------------

    @property
    def mode(self) -> str:
        """'callable', 'attribute', 'wsgi' or 'asgi'.

        Names what is bound, not the operation: a 'callable' binding
        exposes on_call, an 'attribute' binding exposes on_get / on_set /
        on_delete, and the request modes expose on_request. Detected at
        creation, except the request modes, which are always explicit.
        """

        return self._mode

    @property
    def path(self) -> str:
        """Fully qualified location of the bound attribute.

        Format is module:path, both halves dotted. Derived from the
        target and never affected by a label override, so events remain
        self-describing wherever they end up.
        """

        return self._path

    @property
    def label(self) -> str:
        return self._label

    @property
    def target(self) -> Any:
        return self._target

    @property
    def name(self) -> str:
        return self._name

    @property
    def wrapper(self) -> Any:
        """The underlying wrapt handle, or None while unapplied.

        Escape hatch to core wrapt: anything this class does not expose
        remains reachable through it, e.g.
        wrapt.unwrap_object(bnd.target, bnd.name, bnd.wrapper).
        """

        return self._wrapper

    def __repr__(self) -> str:
        if self._wrapper is None:
            state = "unapplied"
        else:
            state = "active" if self.active else "displaced"

        if self._suspended:
            state += " suspended"

        return f"<Binding {self._label!r} {self._mode} {state}>"

    # -- behaviour namespaces ----------------------------------------------

    def _wrong_mode(self, name: str) -> WrongModeError:
        suggestion = {
            "callable": "on_call",
            "attribute": "on_get, on_set or on_delete",
            "wsgi": "on_request",
            "asgi": "on_request",
        }[self._mode]
        article = "an" if self._mode == "attribute" else "a"

        return WrongModeError(
            f"{name} is not available: {self._label} is {article}"
            f" {self._mode!r} binding; use {suggestion}"
        )

    def _namespace(
        self, name: str, wanted: str, factory: type[_BehaviourT]
    ) -> _BehaviourT:
        if self._mode != wanted:
            raise self._wrong_mode(name)

        return factory(self)

    @property
    def on_call(self) -> CallBehaviour:
        """The behaviour namespace for calls. Callable mode only."""

        return self._namespace("on_call", "callable", CallBehaviour)

    @property
    def on_get(self) -> GetBehaviour:
        """The behaviour namespace for attribute reads. Attribute mode only."""

        return self._namespace("on_get", "attribute", GetBehaviour)

    @property
    def on_set(self) -> SetBehaviour:
        """The behaviour namespace for attribute writes. Attribute mode only."""

        return self._namespace("on_set", "attribute", SetBehaviour)

    @property
    def on_delete(self) -> DeleteBehaviour:
        """The behaviour namespace for attribute deletes. Attribute mode only."""

        return self._namespace("on_delete", "attribute", DeleteBehaviour)

    @property
    def on_request(self) -> RequestNamespace:
        """The behaviour namespace for requests. WSGI and ASGI modes only.

        The vocabulary follows the binding's protocol: a wsgi binding
        gets environ and body-iterable stages, an asgi binding gets
        scope and per-chunk stages; the terminals are shared. See
        wrapture.wsgi.RequestBehaviour and
        wrapture.asgi.RequestBehaviour for the accurate signatures.
        """

        if self._mode == "wsgi":
            from .wsgi import RequestBehaviour

            return cast(RequestNamespace, RequestBehaviour(self))

        if self._mode == "asgi":
            from .asgi import RequestBehaviour as ASGIBehaviour

            return cast(RequestNamespace, ASGIBehaviour(self))

        raise self._wrong_mode("on_request")

    # -- lifecycle ---------------------------------------------------------

    @property
    def applied(self) -> bool:
        """Whether apply() installed a wrapper that has not been removed."""

        return self._wrapper is not None

    @property
    def suspended(self) -> bool:
        """Whether an applied wrapper is currently inert.

        Orthogonal to `active`: a suspended binding is still applied, so it
        reports active=True, suspended=True.
        """

        return self._suspended

    @property
    def active(self) -> bool:
        """Whether the wrapper is still installed on the target.

        Queried, not cached, so removal or replacement behind this
        object's back is reported honestly. Three states: unapplied /
        active / displaced.
        """

        if self._wrapper is None:
            return False

        # Resolve what is at the target right now; if the path no longer
        # resolves at all, the wrapper is certainly not installed.

        try:
            current = wrapt.resolve_path(self._target, self._name)[2]
        except Exception:
            return False

        return bool(is_wrapped_by(current, self._wrapper))

    def apply(self, *, suspended: bool = False) -> Self:
        """Apply the wrapper to the target. Returns self, so it chains.

        With suspended=True the wrapper is installed but inert until
        resume() is called.
        """

        if self._wrapper is not None:
            raise AlreadyAppliedError(
                f"{self._label} is already applied. Use either"
                f" `with binding(...)` or apply()/remove() explicitly,"
                f" not both."
            )

        # A fresh apply may warn about missed thread calls again.

        self._gap_warned = False

        if self._mode == "attribute":
            self._wrapper = install_attribute(self, self._target, self._name)
            self._suspended = suspended
            self._apply_count += 1
            _applied_bindings.add(self)
            return self

        # The request modes install their middleware through the same
        # wrap_object path as a callable, so remove() and active work
        # unchanged. The imports are local because both middleware
        # modules import from this module.

        if self._mode == "wsgi":
            from .wsgi import WSGIMiddleware

            def middleware(wrapped: WrappedFunction, *args: Any, **kwargs: Any) -> Any:
                return WSGIMiddleware(wrapped, binding=self)

            self._wrapper = wrapt.wrap_object(self._target, self._name, middleware)
            self._suspended = suspended
            self._apply_count += 1
            _applied_bindings.add(self)
            return self

        if self._mode == "asgi":
            from .asgi import ASGIMiddleware

            def asgi_middleware(
                wrapped: WrappedFunction, *args: Any, **kwargs: Any
            ) -> Any:
                return ASGIMiddleware(wrapped, binding=self)

            self._wrapper = wrapt.wrap_object(self._target, self._name, asgi_middleware)
            self._suspended = suspended
            self._apply_count += 1
            _applied_bindings.add(self)
            return self

        # `enabled` must be supplied at construction: wrapt's _self_enabled
        # is not writable afterwards. When it returns False wrapt bypasses
        # the wrapper entirely.

        def factory(wrapped: WrappedFunction, *args: Any, **kwargs: Any) -> Any:
            return wrapt.FunctionWrapper(wrapped, self._make_wrapper(), self._enabled)

        self._wrapper = wrapt.wrap_object(self._target, self._name, factory)
        self._suspended = suspended
        self._apply_count += 1
        _applied_bindings.add(self)
        return self

    def _enabled(self) -> bool:
        """Read by wrapt on every call; False bypasses the wrapper."""

        if self._suspended:
            self._suspended_calls += 1
            return False

        return True

    def suspend(self) -> Self:
        """Make an applied wrapper inert without removing it.

        The wrapper stays in the chain, so nothing structural changes and
        reconfiguration is atomic from a caller's point of view.
        """

        self._suspended = True
        return self

    def resume(self) -> Self:
        """Reactivate a suspended wrapper."""

        self._suspended = False
        return self

    @property
    def suspended_calls(self) -> int:
        """Calls that reached this binding while it was suspended."""

        return self._suspended_calls

    @property
    def missed_calls(self) -> int:
        """Operations that ran with no recording context while a
        timeline was active elsewhere, typically on a thread, and are
        therefore missing from that timeline's tape."""

        return self._missed_calls

    @property
    def filtered_calls(self) -> int:
        """Operations the `when=` predicate declined to record.

        Deliberate silence, but counted, so a shorter tape than
        expected can be explained rather than guessed at. A static
        `when=False` counts nothing: there is no per-operation
        decision to report.
        """

        return self._filtered_calls

    def _note_missed_call(self) -> None:
        # Count every miss, but warn only once per apply cycle: a
        # worker thread in a loop must not emit thousands of warnings.

        self._missed_calls += 1

        if not self._gap_warned:
            self._gap_warned = True
            warnings.warn(
                f"{self._label}: an observed operation ran on a thread"
                f" with no recording context while a timeline was active"
                f" elsewhere, so it was not recorded (behaviour still"
                f" applied). To record work on this thread, wrap its"
                f" target with wrapture.propagate(...), which hands it a"
                f" copy of the recording context. Misses are counted on"
                f" Binding.missed_calls.",
                RecordingGapWarning,
                stacklevel=2,
            )

    @property
    def events(self) -> EventLog:
        """This binding's events from the enclosing timeline, as a
        filterable EventLog.

        One canonical name across the modes: a callable binding records
        "call" events, an attribute binding records "get", "set" and
        "delete", and the wsgi and asgi bindings record "request";
        narrow with .of_kind() where a mode has several.

        Raises rather than returning an empty log when no events could
        possibly exist, so "recorded nothing" can never be mistaken for
        "not recording": NeverAppliedError if the binding was never
        applied, and RuntimeError outside a timeline.
        """

        if self._apply_count == 0:
            raise NeverAppliedError(
                f"{self._label} was never applied; call apply() or use it"
                f" as a context manager"
            )

        tape = _current_tape()
        if tape is None:
            raise RuntimeError(
                f"{self._label}: events are only recorded inside a timeline()"
            )

        return tape.for_binding(self)

    def remove(self, *, missing_ok: bool = True) -> Self:
        """Remove the wrapper. Idempotent. The binding can be applied
        again afterwards, starting unsuspended."""

        if self._wrapper is None:
            return self

        unwrap_object(self._target, self._name, self._wrapper, missing_ok=missing_ok)

        self._wrapper = None
        self._suspended = False
        _applied_bindings.discard(self)
        return self

    def __enter__(self) -> Self:
        return self.apply()

    def __exit__(self, *exc: object) -> None:
        self.remove()

    # -- declared expectations ---------------------------------------------

    def expect_times(self, count: int) -> Self:
        """Declare that this binding records exactly `count` events.

        Verified when the enclosing timeline exits, so verification
        cannot be forgotten; a mismatch raises ExpectationNotMetError.
        """

        self._expectations.append(("times", count))
        return self

    def expect_once(self) -> Self:
        """Declare that this binding records exactly one event."""

        return self.expect_times(1)

    def expect_never(self) -> Self:
        """Declare that this binding records no events."""

        self._expectations.append(("never", 0))
        return self

    def expect_at_least(self, count: int) -> Self:
        """Declare that this binding records at least `count` events."""

        self._expectations.append(("at_least", count))
        return self

    def _verify(self, tape: Any) -> None:
        # Called by the timeline at exit. Reuses the assertion methods
        # so failure output matches theirs, re-raised under the declared
        # expectation's own exception type.

        if not self._expectations:
            return

        log = tape.for_binding(self)

        for kind, count in self._expectations:
            try:
                if kind == "times":
                    log.assert_times(count)
                elif kind == "never":
                    log.assert_never()
                else:
                    log.assert_at_least(count)
            except AssertionError as exc:
                raise ExpectationNotMetError(
                    f"declared expectation on {self._label} not met: {exc}"
                ) from None

    # -- wrapper -----------------------------------------------------------

    def _make_wrapper(self) -> WrapperFunction:
        bnd = self

        def wrapper(
            wrapped: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            behaviour = bnd._behaviour("call")

            # when=False is a behaviour-only binding: it never records,
            # counts nothing, and takes no part in gap detection.

            if bnd._when is False:
                if behaviour is None:
                    return wrapped(*args, **kwargs)
                return behaviour(wrapped, instance, args, kwargs)

            active = _active_sinks()

            # Not recording: nothing is listening, or this call was
            # triggered by the recording machinery itself rather than by
            # the code under observation. Behaviour still applies; the
            # call is just not recorded, and no event is constructed at
            # all. A call with no listener while a timeline runs
            # elsewhere is a recording gap, typically a thread, and is
            # counted and warned about rather than lost silently.

            if not active or _in_recorder.get():
                if not active and not _in_recorder.get() and _timelines_active():
                    bnd._note_missed_call()

                if behaviour is None:
                    return wrapped(*args, **kwargs)
                return behaviour(wrapped, instance, args, kwargs)

            # The per-call predicate decides whether this operation is
            # recorded at all, before any event is constructed. It runs
            # under the recorder guard, so observed code it consults
            # passes through, and if it raises the caller sees it.

            if bnd._when is not None:
                guard = _in_recorder.set(True)
                try:
                    wanted = bnd._when(instance, args, kwargs)
                finally:
                    _in_recorder.reset(guard)

                if not wanted:
                    bnd._filtered_calls += 1

                    if behaviour is None:
                        return wrapped(*args, **kwargs)
                    return behaviour(wrapped, instance, args, kwargs)

            # Create the event under the recorder guard, so anything
            # the bookkeeping calls that is itself observed passes
            # through instead of recording recursively.

            guard = _in_recorder.set(True)
            try:
                event = bnd._record_call(active, wrapped, instance, args, kwargs)
            finally:
                _in_recorder.reset(guard)

            # Position before delivery: the event is pushed first, so
            # sinks hearing on_enter see its final depth and parent
            # link. Then the call runs with the event on the
            # in-progress stack, so calls made inside the body nest
            # under it.

            base = _stack.get()
            token = _push(event)
            _record_event(event, active)

            # Time from here, after the recording bookkeeping, so its
            # overhead is not charged to the observed code.

            started = time.perf_counter()
            event.started = started

            try:
                if behaviour is None:
                    outcome = wrapped(*args, **kwargs)
                else:
                    outcome = behaviour(
                        _forwarder(wrapped, event), instance, args, kwargs
                    )
            except BaseException as exc:
                event.duration = time.perf_counter() - started
                event.exception = exc
                _notify_error(event, active)
                raise
            finally:
                _pop(token)

            # A generator or coroutine outcome has not run yet: calling
            # the target only constructed it, and the body executes when
            # the consumer iterates or awaits. So the scope above
            # covered construction only, and the outcome is recorded
            # around the iteration or await instead. All tested on the
            # result, not the target: a plain def can return either.

            result_policy = bnd._capture_result
            if result_policy is None:
                result_policy = _required_policy(active, "capture_result")

            if inspect.isgenerator(outcome):
                return _record_generator(outcome, event, base, result_policy, active)

            if inspect.isasyncgen(outcome):
                return _record_async_generator(
                    outcome, event, base, result_policy, active
                )

            if inspect.isawaitable(outcome):
                return _record_awaited(outcome, event, base, result_policy, active)

            event.duration = time.perf_counter() - started
            _capture_result(event, outcome, result_policy)
            _notify_exit(event, active)
            return outcome

        return wrapper

    def _record_call(
        self,
        active: tuple[Sink, ...],
        wrapped: WrappedFunction,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Event:
        # Resolve the argument capture policy: the binding's override,
        # else the highest level the active sinks declare.

        policy = self._capture_args
        if policy is None:
            policy = _required_policy(active, "capture_args")
        level = _level_of(policy)

        event = Event(
            "call",
            self._path,
            label=self._label,
            instance=instance,
            binding=self,
            capture=level,
            injected=self._injected("call"),
        )

        if self._stack_depth is not None:
            event.stack = _capture_stack(self._stack_depth)

        # NONE skips signature binding entirely, the dominant cost of
        # recording: the call stays visible, its values do not.

        if level > NONE:
            arguments = normalized_arguments(wrapped, args, kwargs)

            if not callable(policy) and level == REFERENCE:
                event.args = args
                event.kwargs = kwargs
                event.arguments = arguments
            elif arguments is not None:
                # Above REFERENCE, capture through the normalized form
                # only: keeping the raw call shape too would duplicate
                # every value, and a by-name policy such as redact()
                # cannot see names in a raw args tuple.

                event.arguments = {
                    name: _capture_value(policy, name, value)
                    for name, value in arguments.items()
                }
            else:
                # No signature to normalize against: capture the raw
                # call shape instead, positionals under no name.

                event.args = tuple(
                    _capture_value(policy, None, value) for value in args
                )
                event.kwargs = {
                    name: _capture_value(policy, name, value)
                    for name, value in kwargs.items()
                }

        return event

    # -- behaviour phases -----------------------------------------------------

    def _head(self, operation: str) -> Phase:
        """Phase 0 for the operation, created on first use."""

        head = self._heads.get(operation)

        if head is None:
            head = self._heads[operation] = Phase()
            self._active[operation] = head

        return head

    def _set_terminal(
        self,
        operation: str,
        fn: WrapperFunction,
        *,
        injected: bool = False,
        phase: Phase | None = None,
    ) -> None:
        (phase or self._head(operation)).set_terminal(fn, injected=injected)

    def _add_stage(
        self, operation: str, fn: StageFunction, *, phase: Phase | None = None
    ) -> None:
        (phase or self._head(operation)).add_stage(fn)

    def _clear_behaviour(self, operation: str) -> None:
        self._heads.pop(operation, None)
        self._active.pop(operation, None)

    def _behaviour(self, operation: str) -> WrapperFunction | None:
        """The composed pipeline of the operation's active phase, or None
        when nothing is configured for it."""

        active = self._active.get(operation)

        if active is None:
            return None

        return active.behaviour()

    def _injected(self, operation: str) -> bool:
        """Whether the operation's active behaviour injects its outcome."""

        if operation == "request":
            return self._injects.get("request", False)

        active = self._active.get(operation)
        return active is not None and active.injected


class BindingGroup:
    """Several bindings applied and removed as a unit.

    Bindings are reachable by attribute or item access using the names
    they were given. apply() rolls back on partial failure; remove()
    removes in reverse order of application.
    """

    def __init__(self, points: dict[str, tuple[Any, str] | Binding]) -> None:
        # bindings() supplies (target, name) tuples to construct here,
        # named by the caller; discover() supplies Binding objects it
        # has already configured, keyed by the member name.

        self._bindings: dict[str, Binding] = {}

        for key, point in points.items():
            if isinstance(point, Binding):
                self._bindings[key] = point
            else:
                target, name = point
                self._bindings[key] = Binding(target, name, label=key)

    def __getitem__(self, key: str) -> Binding:
        return self._bindings[key]

    def __iter__(self) -> Any:
        return iter(self._bindings.values())

    def __len__(self) -> int:
        return len(self._bindings)

    def __getattr__(self, key: str) -> Binding:
        try:
            return self._bindings[key]
        except KeyError:
            raise AttributeError(key) from None

    def __repr__(self) -> str:
        return f"<BindingGroup {list(self._bindings)}>"

    @property
    def active(self) -> bool:
        """Whether every binding in the group is applied and active."""

        return all(b.active for b in self._bindings.values())

    @property
    def suspended(self) -> bool:
        """Whether every binding in the group is suspended."""

        return all(b.suspended for b in self._bindings.values())

    def apply(self, *, suspended: bool = False) -> Self:
        """Apply every binding, in declaration order. Returns self.

        If any member fails to apply, the members already applied are
        removed again, so the group never half-applies.
        """

        applied: list[Binding] = []

        try:
            for bnd in self._bindings.values():
                applied.append(bnd.apply(suspended=suspended))
        except Exception:
            for bnd in reversed(applied):
                bnd.remove()
            raise

        return self

    def suspend(self) -> Self:
        """Suspend every binding in the group. Returns self."""

        for bnd in self._bindings.values():
            bnd.suspend()
        return self

    def resume(self) -> Self:
        """Resume every binding in the group. Returns self."""

        for bnd in self._bindings.values():
            bnd.resume()
        return self

    def remove(self) -> Self:
        """Remove every binding, in reverse order of application. Returns
        self. Idempotent, like Binding.remove()."""

        for bnd in reversed(list(self._bindings.values())):
            bnd.remove()
        return self

    def _verify(self, tape: Any) -> None:
        # Called by the timeline at exit: verify every member's declared
        # expectations.

        for bnd in self._bindings.values():
            bnd._verify(tape)

    def __enter__(self) -> Self:
        return self.apply()

    def __exit__(self, *exc: object) -> None:
        self.remove()


def binding(
    target: Any,
    name: str,
    *,
    label: str | None = None,
    mode: str | None = None,
    missing_ok: bool = False,
    capture: CapturePolicy | str | None = None,
    capture_args: CapturePolicy | str | None = None,
    capture_result: CapturePolicy | str | None = None,
    stack: int | str | None = None,
    when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any] | bool | None = None,
) -> Binding:
    """Create a binding for one target attribute.

    `target` is a module, class, instance, or a string: "module" or
    "module:path", the colon convention observe targets, discover()
    and event paths use. `name` is the path from the target to the
    attribute, so "module:Class" with "member" and "module" with
    "Class.member" are the same binding. Prefer the colon form for a
    member owned by a class: point `target` at the owner and keep
    `name` the bare member name, as discover() spells it.

    The mode, 'callable' or 'attribute', is detected from whatever is at
    the target and selects which behaviour namespaces exist. Pass `mode=`
    to override for the ambiguous case of a callable stored as data.
    `mode="wsgi"` and `mode="asgi"` are never detected and must be
    passed explicitly: each wraps an application object of that
    protocol in its recording middleware, records "request" events,
    and offers the on_request namespace; see the wrapture.wsgi and
    wrapture.asgi modules.

    `missing_ok=True` permits binding a name that is not on the class,
    typically one assigned in __init__. Without it such a name raises
    AttributeError, because it is indistinguishable from a typo.

    `capture=` overrides how much of the recorded values this binding
    stores: a level named by string ("none", "types", "reference",
    "summary" or "snapshot"), or a fn(name, value) callable.
    `capture_args=` and `capture_result=` control the two axes
    separately and win over the shorthand. Left unset, the binding
    follows what the sink consuming the events declares.

    `stack=` captures how control reached each recorded event:
    "caller" for just the calling frame, a frame count, or "full" for
    the whole stack. The default None captures nothing and costs
    nothing.

    `when=` decides per operation whether to record it: a callable
    taking (instance, args, kwargs), consulted only while something is
    listening, before any event is constructed. A falsey answer skips
    recording for that operation (behaviour still applies) and counts
    it on `filtered_calls`. The predicate runs in the call path, so it
    should be fast, and if it raises the caller sees the exception.
    For an attribute binding a set passes the written value as the
    single positional argument; a get or delete passes empty args. For a
    wsgi binding the environ mapping is the single positional argument,
    and for an asgi binding the scope mapping is.
    As with wrapt's `enabled`, a boolean is accepted in place of the
    predicate: `when=False` makes a behaviour-only binding that never
    records and counts nothing, for plumbing that must not put itself
    in the trace, and `when=True` is the always-record default.

    Does NOT apply the wrapper; call apply() or use the binding as a
    context manager.
    """

    return Binding(
        target,
        name,
        label=label,
        mode=mode,
        missing_ok=missing_ok,
        capture=capture,
        capture_args=capture_args,
        capture_result=capture_result,
        stack=stack,
        when=when,
    )


def bindings(**points: tuple[Any, str]) -> BindingGroup:
    """Create several bindings at once, named by keyword.

    with bindings(charge=(Gateway, "charge"),
                  ledger=(Ledger, "record")) as group:
        ...
        group.charge.suspend()
    """

    return BindingGroup(dict(points))


def discover(
    target: Any,
    match: str | Sequence[str],
    *,
    exclude: str | Sequence[str] = (),
    capture: CapturePolicy | str | None = None,
    capture_args: CapturePolicy | str | None = None,
    capture_result: CapturePolicy | str | None = None,
    stack: int | str | None = None,
    when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any] | bool | None = None,
) -> BindingGroup:
    """Create bindings for every member of a target matching a pattern.

    `target` is a module, a class, or a string naming one: "module" or
    "module:path". `match` is one fnmatchcase pattern or a sequence of
    them, tried against the target's own immediate member names, and
    `exclude` subtracts from whatever matched. Selection is the same as
    a config file match entry: never inherited members, no traversal
    into nested classes or submodules, only routines the target itself
    defines, skipping properties, other descriptors, plain data and
    anything already wrapped. Use binding() or bindings() to bind any
    of the skipped kinds by exact name.

    The remaining keyword options are the uniform subset of binding()'s
    options, applied to every selected member.

    Discovery enumerates members, so unlike a config observe entry it
    cannot defer: the target must exist when discover() is called, and
    a string target is imported on the spot. A selection that comes up
    empty raises ValueError rather than returning an empty group, so a
    mistyped pattern cannot silently observe nothing.

    Returns a BindingGroup keyed by member name, unapplied: apply() or
    the context-manager form installs the wrappers, exactly as for
    bindings().

        group = discover("shop:OrderService", "place_*")

        with timeline(group):
            ...
            group.place_order.events.assert_once()
    """

    # Normalize the pattern arguments, so a lone string is one pattern
    # rather than a sequence of single characters.

    patterns = (match,) if isinstance(match, str) else tuple(match)
    excludes = (exclude,) if isinstance(exclude, str) else tuple(exclude)

    if not patterns:
        raise ValueError("discover requires at least one match pattern")

    # Resolve a string target now: import the module half and walk the
    # path half to the container whose members are selected from. The
    # created bindings are handed the original spelling; Binding folds
    # a colon target into module plus dotted name itself.

    if isinstance(target, str):
        _reject_deferred(target)

        module_name, _, path = target.partition(":")
        container: Any = importlib.import_module(module_name)
        for part in path.split(".") if path else ():
            container = getattr(container, part)
    else:
        container = target

    # An empty selection is loud: the caller asked for these bindings
    # on the spot, and a typo'd pattern must not vacuously bind nothing.

    members = _select_members(container, patterns, excludes)
    if not members:
        described = (
            target
            if isinstance(target, str)
            else getattr(container, "__name__", None) or repr(container)
        )
        raise ValueError(
            f"discover: match {list(patterns)!r} selected no members of {described!r}"
        )

    return BindingGroup(
        {
            member: Binding(
                target,
                member,
                capture=capture,
                capture_args=capture_args,
                capture_result=capture_result,
                stack=stack,
                when=when,
            )
            for member in members
        }
    )
