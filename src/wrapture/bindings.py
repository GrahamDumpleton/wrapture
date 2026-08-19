"""Bindings: the association of a target attribute with a wrapper.

A binding is created with binding() and names one attribute of a module,
class or instance. Applying it installs a wrapt wrapper on the target;
behaviour configured through the binding's namespaces then applies to every
call until the binding is removed, suspended or reconfigured.
"""

from __future__ import annotations

import importlib
import inspect
import threading
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
from .attributes import is_installed as attribute_installed
from .attributes import uninstall as uninstall_attribute
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
    _Exhausted,
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
from .events import Event, _signature, normalized_arguments
from .exceptions import (
    AlreadyAppliedError,
    DeferredTargetError,
    ExpectationNotMetError,
    NeverAppliedError,
    RecordingGapWarning,
    SequenceExhaustedError,
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
from .values import (
    resolve_owner,
    slot_delete,
    slot_prior,
    slot_read,
    slot_restore,
    slot_write,
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


_BehaviourT = TypeVar("_BehaviourT", bound=_Behaviour[Any])

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


def _location(
    target: Any, attrs: tuple[Any, ...], *, allow_bare: bool = False
) -> tuple[Any, str]:
    """Collapse `binding(target, *attrs)` to wrapt's (owner, "dotted.name").

    `target` is an object, a "module" string or a "module:path" string;
    each further positional is an attribute step, itself possibly
    dotted. The colon in a string target is the module/path boundary,
    since module names contain dots too, and its path half becomes the
    first step. Everything after the owner joins with dots, so
    ("mod", "Class.member"), ("mod", "Class", "member"),
    ("mod:Class", "member") and ("mod:Class.member",) name one location.
    """

    for step in attrs:
        if not isinstance(step, str) or not step:
            raise TypeError(
                f"binding path steps must be non-empty attribute names, got {step!r}"
            )

    steps = list(attrs)

    if isinstance(target, str) and ":" in target:
        target, _, path = target.partition(":")
        if path:
            steps.insert(0, path)

    if not steps:
        if allow_bare:
            return target, ""

        if isinstance(target, str):
            module, _, last = target.rpartition(".")
            hint = (
                f" Use ({module!r}, {last!r}) or {f'{module}:{last}'!r} if"
                f" {last!r} is an attribute of module {module!r}."
                if module
                else ""
            )
            raise TypeError(
                f"binding({target!r}) names a module and no attribute; the"
                f" colon separates the module from the attribute path.{hint}"
            )

        raise TypeError(
            f"binding({target!r}) names no attribute; give the attribute"
            f" path as further arguments, e.g. binding(target, 'name')"
        )

    return target, ".".join(steps)


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
    on_complete: Callable[[Event], None] | None = None,
) -> Any:
    """Record around the await, so the event reflects the real outcome.

    Re-establishes the in-progress stack for the duration, so calls made
    inside the coroutine body nest under this event. The stack is set
    raw rather than pushed, because the event was already linked to its
    parent when the call was recorded. `on_complete` sees the finished
    event, outcome or exception, once the sinks have.
    """

    token = _stack.set(stack + (event,))
    try:
        result = await awaitable
    except BaseException as exc:
        if event.started is not None:
            event.duration = time.perf_counter() - event.started
        event.exception = exc
        _notify_error(event, active)
        if on_complete is not None:
            on_complete(event)
        raise
    finally:
        _stack.reset(token)

    if event.started is not None:
        event.duration = time.perf_counter() - event.started
    _capture_result(event, result, policy)
    _notify_exit(event, active)
    if on_complete is not None:
        on_complete(event)
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
        *attrs: str,
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
        strict: bool = True,
        attr: str | None = None,
        item: Any = MISSING,
    ) -> None:
        # Validate the target and settle the mode before anything is
        # stored, so a bad binding fails on the line that created it.

        _reject_deferred(target)

        # A slot keyword makes this a value binding: the location is the
        # owner, and attr= or item= names the slot in it that is held.

        if attr is not None and item is not MISSING:
            raise TypeError("binding() takes attr= or item=, not both")

        slot = attr is not None or item is not MISSING

        if slot:
            mode = self._check_slot_options(
                attr, item, mode, capture, capture_args, capture_result, stack, when
            )
        elif mode == "value":
            raise ValueError(
                "mode='value' needs a slot: name the owner positionally and"
                " the slot with attr= or item="
            )

        # Collapse the location to an owner and a dotted name, so the
        # colon spelling, the multi-step spelling and the dotted-name
        # spelling of the same member are one binding from here on.

        target, name = _location(target, attrs, allow_bare=slot)

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
        elif mode not in ("callable", "attribute", "wsgi", "asgi", "value"):
            raise ValueError(
                f"mode must be 'callable', 'attribute', 'wsgi' or 'asgi', got {mode!r}"
            )

        # What this binding is bound to.

        self._mode = mode
        self._target = target
        self._name = name
        self._missing_ok = missing_ok

        # The slot of a value binding: its kind, its name or key, the
        # owner it lives in (resolved now, so a bad location fails here),
        # what the binding is configured to hold, and, while applied,
        # what the slot held before.

        self._slot_kind = "attr" if attr is not None else "item"
        self._slot: Any = attr if attr is not None else item
        self._owner: Any = resolve_owner(target, name) if slot else None
        self._holding: tuple[str, Any] = ("through", None)
        self._prior: Any = MISSING
        self._value_applied = False

        if slot:
            self._path = self._slot_path(target, name)
            self._label = label or self._slot_label(target, name)
        else:
            self._path = _derive_path(target, name)
            self._label = label or self._default_label(target, name)

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

        # Whether a call that behaviour answers without reaching the
        # real callable is still checked against its signature.

        self._strict = strict

        # The behaviour phases, keyed by operation ("call", "get", "set"
        # or "delete"): each operation has a chain of Phase records
        # starting at its head, and the phase currently deciding what
        # the operation does. Both are created on first use. Request
        # behaviour is stage-keyed rather than composed, so it lives in
        # its own structure, consumed by the WSGI and ASGI middlewares.

        self._heads: dict[str, Phase] = {}
        self._active: dict[str, Phase] = {}
        self._phase_lock = threading.Lock()
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

    @staticmethod
    def _check_slot_options(
        attr: str | None,
        item: Any,
        mode: str | None,
        *recording: Any,
    ) -> str:
        # A slot is value mode unless item= says otherwise. attr= accepts
        # no mode, since wrapping what an attribute holds is already the
        # positional spelling; item= accepts the modes that have no other
        # way to reach a mapping entry: a callable or an application held
        # in it. A value binding records nothing, so for it the recording
        # options are refused rather than silently ignored.

        if attr is not None and mode is not None:
            raise TypeError(
                f"attr= is a value slot and takes no mode= (got {mode!r}); to"
                f" wrap what the attribute holds, name it positionally:"
                f" binding(owner, {attr!r})"
            )

        if item is not MISSING and mode not in (
            None,
            "value",
            "callable",
            "wsgi",
            "asgi",
        ):
            if mode == "attribute":
                raise TypeError(
                    "a mapping entry is not a descriptor slot, so item= cannot"
                    " take mode='attribute'; bind the attribute on its class"
                )
            raise ValueError(
                f"mode must be 'value', 'callable', 'wsgi' or 'asgi' for item=,"
                f" got {mode!r}"
            )

        if mode is None or mode == "value":
            if any(option is not None for option in recording):
                raise ValueError(
                    "a value binding records nothing, so capture=, capture_args=,"
                    " capture_result=, stack= and when= do not apply to it"
                )
            return "value"

        return mode

    def _slot_label(self, target: Any, name: str) -> str:
        # An owner given as an object with no name of its own (an
        # instance, os.environ) is labelled by its type: a repr can be
        # arbitrarily long and, carrying an address, unstable.

        if name:
            owner = self._default_label(target, name)
        elif isinstance(target, str):
            owner = target
        else:
            owner = getattr(target, "__name__", None) or type(target).__qualname__

        if self._slot_kind == "attr":
            return f"{owner}.{self._slot}"

        return f"{owner}[{self._slot!r}]"

    def _slot_path(self, target: Any, name: str) -> str:
        owner = _derive_path(target, name).rstrip(".")

        if self._slot_kind == "attr":
            separator = "" if owner.endswith(":") else "."
            return f"{owner}{separator}{self._slot}"

        return f"{owner}[{self._slot!r}]"

    # -- identity ----------------------------------------------------------

    @property
    def mode(self) -> str:
        """'callable', 'attribute', 'wsgi', 'asgi' or 'value'.

        Names what is bound, not the operation: a 'callable' binding
        exposes on_call, an 'attribute' binding exposes on_get / on_set /
        on_delete, the request modes expose on_request, and a 'value'
        binding (a slot named with attr= or item=) exposes overrides(),
        hides() and passes_through(). Detected at creation, except the
        request modes, which are always explicit, and value mode, which
        the slot keyword selects.
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
        if not self.applied:
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
            "value": "overrides(), hides() or passes_through()",
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
        """Whether apply() installed a wrapper, or a value binding wrote
        its slot, and remove() has not been called since."""

        return self._wrapper is not None or self._value_applied

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

        if self._mode == "value":
            return self._value_applied and self._slot_as_configured()

        if self._wrapper is None:
            return False

        # An attribute descriptor may sit on a per-module class rather
        # than at the target path itself, so the attribute module finds
        # it. Otherwise resolve what is at the target right now; if the
        # path no longer resolves at all, the wrapper is certainly not
        # installed. A mapping entry is read directly.

        if self._mode == "attribute":
            return attribute_installed(self._target, self._name, self._wrapper)

        if self._slot_kind == "item" and self._owner is not None:
            current = slot_read(self._owner, "item", self._slot)
        else:
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

        if self.applied:
            raise AlreadyAppliedError(
                f"{self._label} is already applied. Use either"
                f" `with binding(...)` or apply()/remove() explicitly,"
                f" not both."
            )

        # A value binding has no wrapper: it notes what the slot holds
        # and writes what it is configured to hold. Nothing else below
        # applies to it.

        if self._mode == "value":
            self._prior = slot_prior(self._owner, self._slot_kind, self._slot)

            # Write before marking applied, so a slot that refuses the
            # value (os.environ given a non-string) leaves nothing to
            # undo and the binding unapplied.

            if not suspended:
                self._write_slot()

            self._value_applied = True
            self._suspended = suspended
            self._apply_count += 1
            _applied_bindings.add(self)
            return self

        # Behaviour restarts from phase 0 on every apply.

        self._restart_phases()

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

            self._wrapper = self._install(middleware)
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

            self._wrapper = self._install(asgi_middleware)
            self._suspended = suspended
            self._apply_count += 1
            _applied_bindings.add(self)
            return self

        # `enabled` must be supplied at construction: wrapt's _self_enabled
        # is not writable afterwards. When it returns False wrapt bypasses
        # the wrapper entirely.

        def factory(wrapped: WrappedFunction, *args: Any, **kwargs: Any) -> Any:
            return wrapt.FunctionWrapper(wrapped, self._make_wrapper(), self._enabled)

        self._wrapper = self._install(factory)
        self._suspended = suspended
        self._apply_count += 1
        _applied_bindings.add(self)
        return self

    def _install(self, factory: Callable[..., Any]) -> Any:
        """Wrap what the location holds and store the wrapper back there.

        An attribute location goes through wrapt.wrap_object(); a mapping
        entry (item=) is read, wrapped and written back directly, with
        the original kept for remove().
        """

        if self._slot_kind != "item" or self._owner is None:
            return wrapt.wrap_object(self._target, self._name, factory)

        original = slot_read(self._owner, "item", self._slot)
        if original is MISSING:
            raise KeyError(
                f"{self._label}: the mapping has no entry {self._slot!r} to wrap"
            )

        wrapper = factory(original)
        slot_write(self._owner, "item", self._slot, wrapper)
        self._prior = original
        return wrapper

    def _uninstall(self, *, missing_ok: bool) -> None:
        """Put the original back where the wrapper was."""

        if self._mode == "attribute":
            uninstall_attribute(
                self._target, self._name, self._wrapper, missing_ok=missing_ok
            )
            return

        if self._slot_kind != "item" or self._owner is None:
            unwrap_object(
                self._target, self._name, self._wrapper, missing_ok=missing_ok
            )
            return

        current = slot_read(self._owner, "item", self._slot)
        if current is self._wrapper:
            slot_write(self._owner, "item", self._slot, self._prior)
        elif not missing_ok:
            raise ValueError(
                f"{self._label}: the mapping entry no longer holds the wrapper"
            )

        self._prior = MISSING

    def _enabled(self) -> bool:
        """Read by wrapt on every call; False bypasses the wrapper."""

        if self._suspended:
            self._suspended_calls += 1
            return False

        return True

    def suspend(self) -> Self:
        """Make an applied wrapper inert without removing it.

        The wrapper stays in the chain, so nothing structural changes and
        reconfiguration is atomic from a caller's point of view. A value
        binding puts the slot's prior state back until resume().
        """

        if self._mode == "value" and self._value_applied and not self._suspended:
            slot_restore(self._owner, self._slot_kind, self._slot, self._prior)

        self._suspended = True
        return self

    def resume(self) -> Self:
        """Reactivate a suspended wrapper; a value binding writes its slot
        again."""

        if self._mode == "value" and self._value_applied and self._suspended:
            self._suspended = False
            self._write_slot()
            return self

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

        if self._mode == "value":
            raise WrongModeError(
                f"{self._label} is a value binding and records nothing; there"
                f" are no events to read"
            )

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

        if self._mode == "value":
            if self._value_applied:
                if not self._suspended:
                    slot_restore(self._owner, self._slot_kind, self._slot, self._prior)
                self._value_applied = False
                self._suspended = False
                self._prior = MISSING
                _applied_bindings.discard(self)
            return self

        if self._wrapper is None:
            return self

        self._uninstall(missing_ok=missing_ok)

        self._wrapper = None
        self._suspended = False
        _applied_bindings.discard(self)
        return self

    def __enter__(self) -> Self:
        return self.apply()

    def __exit__(self, *exc: object) -> None:
        self.remove()

    # -- value bindings ----------------------------------------------------

    def _value_only(self, verb: str) -> None:
        if self._mode != "value":
            raise WrongModeError(
                f"{verb}() is only available on a value binding (a slot named"
                f" with attr= or item=); {self._label} is a {self._mode!r}"
                f" binding"
            )

    def overrides(self, value: Any) -> Self:
        """Hold `value` in the slot while applied. Value bindings only.

        Settable before or after apply(); on an applied binding the slot
        is written at once. Returns self, so it chains into apply() or
        the context manager.
        """

        self._value_only("overrides")
        self._holding = ("overrides", value)
        self._write_slot_if_live()
        return self

    def hides(self) -> Self:
        """Keep the slot absent while applied: the environment variable
        unset, the key not in the mapping, the attribute not on the
        owner. Value bindings only. Returns self."""

        self._value_only("hides")
        self._holding = ("hides", None)
        self._write_slot_if_live()
        return self

    def passes_through(self) -> Self:
        """Leave the slot as it really is: the initial state, and the way
        back to it on an applied binding without removing it. Value
        bindings only. Returns self."""

        self._value_only("passes_through")
        self._holding = ("through", None)
        self._write_slot_if_live()
        return self

    def _write_slot_if_live(self) -> None:
        if self._value_applied and not self._suspended:
            self._write_slot()

    def _write_slot(self) -> None:
        # Put the slot into the configured state, relative to what it
        # held when the binding was applied.

        state, value = self._holding

        if state == "overrides":
            slot_write(self._owner, self._slot_kind, self._slot, value)
        elif state == "hides":
            slot_delete(self._owner, self._slot_kind, self._slot)
        else:
            slot_restore(self._owner, self._slot_kind, self._slot, self._prior)

    def _slot_as_configured(self) -> bool:
        # Whether the slot still holds what the binding put there. A
        # suspended binding put the prior back, so that is what counts;
        # equality is the fallback for immutables such as the strings
        # os.environ hands back.

        state, value = self._holding
        current = slot_read(self._owner, self._slot_kind, self._slot)

        if self._suspended or state == "through":
            expected = self._prior
        elif state == "hides":
            return current is MISSING
        else:
            expected = value

        if current is MISSING or expected is MISSING:
            return current is expected

        return bool(current is expected or current == expected)

    # -- declared expectations ---------------------------------------------

    def _expects(self, kind: str, count: int) -> Self:
        if self._mode == "value":
            raise WrongModeError(
                f"{self._label} is a value binding and records nothing; it"
                f" cannot carry an expectation"
            )

        self._expectations.append((kind, count))
        return self

    def expect_times(self, count: int) -> Self:
        """Declare that this binding records exactly `count` events.

        Verified when the enclosing timeline exits, so verification
        cannot be forgotten; a mismatch raises ExpectationNotMetError.
        """

        return self._expects("times", count)

    def expect_once(self) -> Self:
        """Declare that this binding records exactly one event."""

        return self.expect_times(1)

    def expect_never(self) -> Self:
        """Declare that this binding records no events."""

        return self._expects("never", 0)

    def expect_at_least(self, count: int) -> Self:
        """Declare that this binding records at least `count` events."""

        return self._expects("at_least", count)

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
            if bnd._strict:
                bnd._check_signature(wrapped, args, kwargs)

            phase = bnd._select("call")

            # when=False is a behaviour-only binding: it never records,
            # counts nothing, and takes no part in gap detection.

            if bnd._when is False:
                return bnd._quiet("call", phase, wrapped, instance, args, kwargs)

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

                return bnd._quiet("call", phase, wrapped, instance, args, kwargs)

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
                    return bnd._quiet("call", phase, wrapped, instance, args, kwargs)

            # Create the event under the recorder guard, so anything
            # the bookkeeping calls that is itself observed passes
            # through instead of recording recursively.

            guard = _in_recorder.set(True)
            try:
                event = bnd._record_call(active, wrapped, instance, args, kwargs, phase)
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

            slot: list[Phase | None] = [phase]

            try:
                outcome = bnd._invoke(
                    "call",
                    phase,
                    wrapped,
                    instance,
                    args,
                    kwargs,
                    event,
                    via=_forwarder(wrapped, event),
                    slot=slot,
                )
            except BaseException as exc:
                event.duration = time.perf_counter() - started
                event.exception = exc
                _notify_error(event, active)
                bnd._completed("call", slot[0], event)
                raise
            finally:
                _pop(token)

            phase = slot[0]

            # A generator or coroutine outcome has not run yet: calling
            # the target only constructed it, and the body executes when
            # the consumer iterates or awaits. So the scope above
            # covered construction only, and the outcome is recorded
            # around the iteration or await instead. All tested on the
            # result, not the target: a plain def can return either.

            result_policy = bnd._capture_result
            if result_policy is None:
                result_policy = _required_policy(active, "capture_result")

            # An until= predicate needs the outcome whatever the sinks
            # asked for, so capture at least by reference for it.

            watching = phase is not None and phase.watches
            if watching and _level_of(result_policy) < REFERENCE:
                result_policy = REFERENCE

            def completed(event: Event) -> None:
                bnd._completed("call", phase, event)

            if inspect.isgenerator(outcome):
                completed(event)
                return _record_generator(outcome, event, base, result_policy, active)

            if inspect.isasyncgen(outcome):
                completed(event)
                return _record_async_generator(
                    outcome, event, base, result_policy, active
                )

            if inspect.isawaitable(outcome):
                return _record_awaited(
                    outcome,
                    event,
                    base,
                    result_policy,
                    active,
                    completed if watching else None,
                )

            event.duration = time.perf_counter() - started
            _capture_result(event, outcome, result_policy)
            _notify_exit(event, active)
            completed(event)
            return outcome

        return wrapper

    def _record_call(
        self,
        active: tuple[Sink, ...],
        wrapped: WrappedFunction,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        phase: Phase | None,
    ) -> Event:
        # Resolve the argument capture policy: the binding's override,
        # else the highest level the active sinks declare.

        policy = self._capture_args
        if policy is None:
            policy = _required_policy(active, "capture_args")

        # An until= predicate needs the arguments whatever the sinks
        # asked for, so capture at least by reference for it.

        if phase is not None and phase.watches and _level_of(policy) < REFERENCE:
            policy = REFERENCE

        level = _level_of(policy)

        event = Event(
            "call",
            self._path,
            label=self._label,
            instance=instance,
            binding=self,
            capture=level,
            injected=phase is not None and phase.injected,
            phase=self._phase_of(phase),
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

    def _then(
        self, operation: str, phase: Phase, exit: tuple[str, Any] | None
    ) -> Phase:
        """Create or fetch the successor of `phase`, recording the exit
        condition that hands over to it."""

        with self._phase_lock:
            if phase.successor is None:
                phase.successor = Phase(phase.index + 1)
                phase.exit = exit
            elif exit is not None:
                phase.exit = exit

            return phase.successor

    def _select(self, operation: str) -> Phase | None:
        """The phase that handles the operation now, or None when nothing
        is configured.

        Choosing the phase also counts the operation against a count exit,
        so that exactly n operations run under a phase whose exit is
        after=n, even with concurrent callers: the phase is chosen and
        counted under the lock, and a count reached by this operation
        moves subsequent operations on while this one keeps its phase.
        """

        active = self._active.get(operation)

        # The common case, one phase or the last of the chain, needs no
        # counting and no lock.

        if active is None or active.successor is None:
            return active

        with self._phase_lock:
            active = self._active[operation]
            active.handled += 1

            if (
                active.exit is not None
                and active.exit[0] == "after"
                and active.handled >= active.exit[1]
                and active.successor is not None
            ):
                self._active[operation] = active.successor

            return active

    def _invoke(
        self,
        operation: str,
        phase: Phase | None,
        wrapped: WrappedFunction,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        event: Event | None = None,
        via: WrappedFunction | None = None,
        slot: list[Phase | None] | None = None,
    ) -> Any:
        """Run the operation under `phase`, or the real operation when
        there is none.

        Behaviour receives `via` in place of `wrapped` when given: the
        recording path hands over a forwarder that notes what behaviour
        actually passed on. A returns_from() sequence that runs out ends
        its phase here: the operation is re-dispatched to the successor,
        the event, when one is being recorded, is restamped with the
        phase that actually handled it, and `slot`, when given, is
        updated so the caller learns which phase that was.
        """

        while True:
            behaviour = None if phase is None else phase.behaviour()

            try:
                if behaviour is None:
                    return wrapped(*args, **kwargs)
                return behaviour(via or wrapped, instance, args, kwargs)
            except _Exhausted:
                assert phase is not None
                phase = self._exhausted(operation, phase)

                if event is not None:
                    event.injected = phase.injected
                    event.phase = self._phase_of(phase)
                if slot is not None:
                    slot[0] = phase

    def _check_signature(
        self, wrapped: WrappedFunction, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        """Reject a call that does not fit the callable's signature when
        behaviour would answer it without the real callable ever seeing
        it.

        A phase with a terminal (returns, raises, returns_from or
        decorates) may do exactly that, so the call shape is checked
        first, raising TypeError as the real call would; a phase without
        one runs the real callable, which does its own checking. The
        check runs before the call is counted, drawn from a sequence, or
        recorded, so a rejected call leaves no trace, and targets with
        no obtainable signature are not checked.
        """

        active = self._active.get("call")
        if active is None or active.terminal is None:
            return

        signature = _signature(wrapped)
        if signature is None:
            return

        try:
            signature.bind(*args, **kwargs)
        except TypeError as exc:
            raise TypeError(f"{self._label} (stubbed): {exc}") from None

    def _completed(self, operation: str, phase: Phase | None, event: Event) -> None:
        """Show a completed operation to the phase's until= predicate,
        handing over to the successor when it says so.

        The predicate runs under the recorder guard, like when=, so
        observed code it consults does not record; if it raises, the
        caller of the operation sees the exception.
        """

        if phase is None or not phase.watches or phase.successor is None:
            return

        assert phase.exit is not None
        predicate = phase.exit[1]

        guard = _in_recorder.set(True)
        try:
            finished = predicate(event)
        finally:
            _in_recorder.reset(guard)

        if not finished:
            return

        with self._phase_lock:
            if self._active.get(operation) is phase:
                self._active[operation] = phase.successor

    def _quiet(
        self,
        operation: str,
        phase: Phase | None,
        wrapped: WrappedFunction,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Run an unrecorded call: nothing is listening, or the binding or
        its when= excluded it.

        A phase with an until= exit still needs to see the completed
        call, so one is built for it privately, never delivered to a
        sink, capturing by reference. The cost is paid only while such a
        phase is active.
        """

        slot: list[Phase | None] = [phase]

        try:
            outcome = self._invoke(
                operation, phase, wrapped, instance, args, kwargs, slot=slot
            )
        except BaseException as exc:
            final = slot[0]

            if final is not None and final.watches:
                event = self._record_call((), wrapped, instance, args, kwargs, final)
                event.exception = exc
                self._completed(operation, final, event)

            raise

        final = slot[0]
        if final is None or not final.watches:
            return outcome

        event = self._record_call((), wrapped, instance, args, kwargs, final)

        # A coroutine has not run yet, so the predicate sees it once it
        # resolves; a generator is shown at construction, with no result.

        if inspect.isawaitable(outcome):
            return self._quiet_awaited(operation, final, outcome, event)

        if not inspect.isgenerator(outcome) and not inspect.isasyncgen(outcome):
            event.result = outcome

        self._completed(operation, final, event)
        return outcome

    async def _quiet_awaited(
        self, operation: str, phase: Phase, awaitable: Any, event: Event
    ) -> Any:
        try:
            result = await awaitable
        except BaseException as exc:
            event.exception = exc
            self._completed(operation, phase, event)
            raise

        event.result = result
        self._completed(operation, phase, event)
        return result

    def _exhausted(self, operation: str, phase: Phase) -> Phase:
        """Hand over from a phase whose sequence ran out, returning the
        phase that takes the operation."""

        with self._phase_lock:
            if phase.successor is None:
                raise SequenceExhaustedError(
                    f"{self._label}: the returns_from() sequence of phase"
                    f" {phase.index} is exhausted and no phase follows it;"
                    f" add one with then() or supply an endless sequence"
                )

            if self._active.get(operation) is phase:
                self._active[operation] = phase.successor

        successor = self._select(operation)
        assert successor is not None
        return successor

    def _advance(self, operation: str) -> bool:
        """Move the operation to its successor phase; False when it is
        already on the last one."""

        with self._phase_lock:
            active = self._active.get(operation)

            if active is None or active.successor is None:
                return False

            self._active[operation] = active.successor
            return True

    def _restart_phases(self) -> None:
        """Return every operation to phase 0 with fresh counters."""

        with self._phase_lock:
            for operation, head in self._heads.items():
                self._active[operation] = head

                phase: Phase | None = head
                while phase is not None:
                    phase.restart()
                    phase = phase.successor

    def _phase_index(self, operation: str) -> int:
        active = self._active.get(operation)
        return 0 if active is None else active.index

    def _phased(self) -> list[str]:
        """The operations that have more than one phase."""

        return [
            operation
            for operation, head in self._heads.items()
            if head.successor is not None
        ]

    @property
    def phase(self) -> int:
        """Index of the phase currently deciding the binding's behaviour.

        Zero for a binding that never called then(). An attribute binding
        with phases on more than one operation has no single answer; ask
        the namespace instead (`on_get.phase`).
        """

        phased = self._phased()

        if len(phased) > 1:
            raise ValueError(
                f"{self._label} has phases on {', '.join(sorted(phased))};"
                f" use the operation's namespace, e.g. on_get.phase"
            )

        return self._phase_index(phased[0]) if phased else 0

    def advance(self) -> Self:
        """Move every operation with phases on to its next phase, whatever
        the current phase's exit condition. A no-op on the last phase.
        Returns self."""

        for operation in self._phased():
            self._advance(operation)

        return self

    @staticmethod
    def _phase_of(phase: Phase | None) -> int | None:
        """The phase index to stamp on an event: None unless the operation
        actually has more than one phase, so unphased bindings record
        nothing new."""

        if phase is None or (phase.index == 0 and phase.successor is None):
            return None

        return phase.index


class BindingGroup:
    """Several bindings applied and removed as a unit.

    Bindings are reachable by attribute or item access using the names
    they were given. apply() rolls back on partial failure; remove()
    removes in reverse order of application.
    """

    def __init__(self, members: dict[str, Binding]) -> None:
        # bindings() supplies bindings named by the caller; discover()
        # supplies the bindings it constructed, keyed by member name. A
        # group never relabels what it is handed: the key is the name it
        # is reached by, the label is the binding's own.

        for key, member in members.items():
            if not isinstance(member, Binding):
                raise TypeError(
                    f"bindings() takes Binding instances, got {member!r} for"
                    f" {key!r}; construct each member with binding()"
                )

        self._bindings: dict[str, Binding] = dict(members)

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

    def advance(self) -> Self:
        """Advance every binding in the group to its next phase, so a set
        of stand-ins changes regime together. Returns self."""

        for bnd in self._bindings.values():
            bnd.advance()
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
    *attrs: str,
    label: str | None = None,
    mode: str | None = None,
    missing_ok: bool = False,
    capture: CapturePolicy | str | None = None,
    capture_args: CapturePolicy | str | None = None,
    capture_result: CapturePolicy | str | None = None,
    stack: int | str | None = None,
    when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any] | bool | None = None,
    strict: bool = True,
    attr: str | None = None,
    item: Any = MISSING,
) -> Binding:
    """Create a binding for one target attribute, or for a slot in it.

    The positional arguments name a location. `target` is a module,
    class, instance, or a string: "module" or "module:path", the colon
    convention observe targets, discover() and event paths use. Each
    further positional string is an attribute step from there, itself
    possibly dotted, so binding("mod", "Class.member"),
    binding("mod", "Class", "member"), binding("mod:Class", "member")
    and binding("mod:Class.member") are the same binding. In a string
    the colon is the module/path boundary, since module names contain
    dots too; "os.path.join" alone names no attribute. Prefer the
    colon form for a member owned by a class: point `target` at the
    owner and keep the last step the bare member name, as discover()
    spells it.

    With `attr=` or `item=` the location is instead the owner of a
    slot, and the binding is a value binding: it holds a value in the
    slot (`overrides(value)`), keeps the slot absent (`hides()`), or
    leaves it alone (`passes_through()`), for as long as it is applied,
    restoring the prior state on remove(). `attr=` names an attribute
    of the owner, `item=` a mapping entry (any key: an os.environ
    variable, a dict key, a sys.modules name, a list index). The owner
    is never replaced, so every reference to it sees the change. Such
    a binding records nothing and has no behaviour namespaces; the
    recording options below do not apply and are refused. `attr=`
    takes no mode= (to wrap what an attribute holds, name it
    positionally). `item=` also takes mode="callable", "wsgi" or
    "asgi", since nothing else can reach a callable or an application
    held in a mapping (a handler in a dispatch table, an app in a
    registry): the entry is wrapped in place, with the whole vocabulary
    of that mode, and the original put back on remove().

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

    `strict=` (default True) checks each call that behaviour answers
    without reaching the real callable (a phase with a terminal:
    returns, raises, returns_from or decorates) against the callable's
    signature, raising TypeError as the real call would, so a call
    site that drifts from the signature cannot pass on the strength
    of a stub. Calls that reach the real callable are checked by it,
    and targets with no obtainable signature are not checked. Pass
    strict=False for a patch that deliberately accepts a different
    call shape.

    Does NOT apply the wrapper; call apply() or use the binding as a
    context manager.
    """

    return Binding(
        target,
        *attrs,
        label=label,
        mode=mode,
        missing_ok=missing_ok,
        capture=capture,
        capture_args=capture_args,
        capture_result=capture_result,
        stack=stack,
        when=when,
        strict=strict,
        attr=attr,
        item=item,
    )


def bindings(**members: Binding) -> BindingGroup:
    """Group bindings under names, to apply and remove as one unit.

    Each member is a binding constructed with binding(), so it carries
    its own mode and options; the keyword is the name it is reached by
    on the group. The group applies in declaration order, rolling back
    if a later member fails, removes in reverse, and suspends, resumes
    and advances every member together:

        with bindings(charge=binding(Gateway, "charge"),
                      ledger=binding(Ledger, "record")) as group:
            ...
            group.charge.suspend()
    """

    return BindingGroup(dict(members))


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
    strict: bool = True,
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
                strict=strict,
            )
            for member in members
        }
    )
