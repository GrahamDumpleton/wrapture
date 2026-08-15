"""Bindings: the association of a target attribute with a wrapper.

A binding is created with binding() and names one attribute of a module,
class or instance. Applying it installs a wrapt wrapper on the target;
behaviour configured through the binding's namespaces then applies to every
call until the binding is removed, suspended or reconfigured.
"""

from __future__ import annotations

import inspect
import types
from typing import Any, Self, TypeVar

import wrapt
from wrapt import is_wrapped_by, unwrap_object

from .attributes import install as install_attribute
from .behaviours import (
    CallBehaviour,
    DeleteBehaviour,
    GetBehaviour,
    SetBehaviour,
    StageFunction,
    WrappedFunction,
    WrapperFunction,
    _Behaviour,
    _compose,
)
from .capture import (
    NONE,
    REFERENCE,
    CapturePolicy,
    _capture_value,
    _level_of,
)
from .eventlogs import EventLog
from .events import Event, normalized_arguments
from .exceptions import (
    AlreadyAppliedError,
    DeferredTargetError,
    ExpectationNotMetError,
    NeverAppliedError,
    WrongModeError,
)
from .timeline import Tape, _in_recorder, _pop, _push, _stack, _tape

_BehaviourT = TypeVar("_BehaviourT", bound=_Behaviour)


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
            f" first and bind against it."
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
    policy: CapturePolicy = REFERENCE,
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
        event.exception = exc
        raise
    finally:
        _stack.reset(token)

    _capture_result(event, result, policy)
    return result


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
        capture: CapturePolicy | None = None,
        capture_args: CapturePolicy | None = None,
        capture_result: CapturePolicy | None = None,
    ) -> None:
        # Validate the target and settle the mode before anything is
        # stored, so a bad binding fails on the line that created it.

        _reject_deferred(target)

        if mode is None:
            mode = _detect_mode(target, name, missing_ok=missing_ok)
        elif mode not in ("callable", "attribute"):
            raise ValueError(f"mode must be 'callable' or 'attribute', got {mode!r}")

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

        self._capture_args = capture_args if capture_args is not None else capture
        self._capture_result = capture_result if capture_result is not None else capture

        # The behaviour pipelines, keyed by operation ("call", "get",
        # "set" or "delete"): composing stages around one terminal, with
        # the composed form cached until either changes.

        self._pipelines: dict[str, list[StageFunction]] = {}
        self._terminals: dict[str, WrapperFunction] = {}
        self._composed: dict[str, WrapperFunction] = {}

        # Lifecycle state, populated by apply() and cleared by remove().
        # The apply count survives remove(): it distinguishes a binding
        # that recorded nothing from one that was never applied at all.

        self._wrapper: Any = None
        self._suspended = False
        self._suspended_calls = 0
        self._apply_count = 0

        # Declared expectations, verified by the enclosing timeline at
        # exit. Like behaviour, they persist across apply/remove cycles.

        self._expectations: list[tuple[str, int]] = []

        # Which operations currently have an injecting terminal
        # (returns / raises / rejects), so their events can be marked.

        self._injects: dict[str, bool] = {}

    @staticmethod
    def _default_label(target: Any, name: str) -> str:
        owner = getattr(target, "__name__", None) or repr(target)
        return f"{owner}.{name}"

    # -- identity ----------------------------------------------------------

    @property
    def mode(self) -> str:
        """'callable' or 'attribute'. Detected at creation.

        Names what is bound, not the operation: a 'callable' binding
        exposes on_call, an 'attribute' binding exposes on_get / on_set /
        on_delete.
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

    def _namespace(
        self, name: str, wanted: str, factory: type[_BehaviourT]
    ) -> _BehaviourT:
        if self._mode != wanted:
            other = "on_get, on_set or on_delete" if wanted == "callable" else "on_call"
            article = "an" if self._mode == "attribute" else "a"
            raise WrongModeError(
                f"{name} is not available: {self._label} is {article}"
                f" {self._mode!r} binding; use {other}"
            )

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

        if self._mode == "attribute":
            self._wrapper = install_attribute(self, self._target, self._name)
            self._suspended = suspended
            self._apply_count += 1
            return self

        # `enabled` must be supplied at construction: wrapt's _self_enabled
        # is not writable afterwards. When it returns False wrapt bypasses
        # the wrapper entirely.

        def factory(wrapped: WrappedFunction, *args: Any, **kwargs: Any) -> Any:
            return wrapt.FunctionWrapper(wrapped, self._make_wrapper(), self._enabled)

        self._wrapper = wrapt.wrap_object(self._target, self._name, factory)
        self._suspended = suspended
        self._apply_count += 1
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
    def events(self) -> EventLog:
        """This binding's events from the enclosing timeline, as a
        filterable EventLog.

        One canonical name across both modes: a callable binding records
        "call" events, an attribute binding records "get", "set" and
        "delete"; narrow with .of_kind() where a mode has several.

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

        tape = _tape.get()
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
            tape = _tape.get()

            # Not recording: no timeline is active, or this call was
            # triggered by the recording machinery itself rather than by
            # the code under observation. Behaviour still applies; the
            # call is just not recorded.

            if tape is None or _in_recorder.get():
                if behaviour is None:
                    return wrapped(*args, **kwargs)
                return behaviour(wrapped, instance, args, kwargs)

            # Create and record the event under the recorder guard, so
            # anything the bookkeeping calls that is itself observed
            # passes through instead of recording recursively.

            guard = _in_recorder.set(True)
            try:
                event = bnd._record_call(tape, wrapped, instance, args, kwargs)
            finally:
                _in_recorder.reset(guard)

            # Run the call with the event on the in-progress stack, so
            # calls made inside the body nest under it.

            base = _stack.get()
            token = _push(event)
            try:
                if behaviour is None:
                    outcome = wrapped(*args, **kwargs)
                else:
                    outcome = behaviour(
                        _forwarder(wrapped, event), instance, args, kwargs
                    )
            except BaseException as exc:
                event.exception = exc
                raise
            finally:
                _pop(token)

            # Calling an `async def` does not run it: it returns a
            # coroutine immediately, and the body only executes when
            # something awaits it. So the scope above covered
            # construction only, and the outcome must be recorded around
            # the await instead. Tested on the result, not the target: a
            # plain def can return an awaitable too.

            result_policy = bnd._capture_result
            if result_policy is None:
                result_policy = getattr(tape, "capture_result", REFERENCE)

            if inspect.isawaitable(outcome):
                return _record_awaited(outcome, event, base, result_policy)

            _capture_result(event, outcome, result_policy)
            return outcome

        return wrapper

    def _record_call(
        self,
        tape: Tape,
        wrapped: WrappedFunction,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Event:
        # Resolve the argument capture policy: the binding's override,
        # else what the sink consuming the events declares.

        policy = self._capture_args
        if policy is None:
            policy = getattr(tape, "capture_args", REFERENCE)
        level = _level_of(policy)

        event = Event(
            "call",
            self._path,
            label=self._label,
            instance=instance,
            binding=self,
            capture=level,
            injected=self._injects.get("call", False),
        )

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

        return tape.record(event)

    # -- behaviour pipelines -------------------------------------------------

    def _set_terminal(
        self, operation: str, fn: WrapperFunction, *, injected: bool = False
    ) -> None:
        self._terminals[operation] = fn
        self._injects[operation] = injected
        self._composed.pop(operation, None)

    def _add_stage(self, operation: str, fn: StageFunction) -> None:
        self._pipelines.setdefault(operation, []).append(fn)
        self._composed.pop(operation, None)

    def _clear_behaviour(self, operation: str) -> None:
        self._pipelines.pop(operation, None)
        self._terminals.pop(operation, None)
        self._injects.pop(operation, None)
        self._composed.pop(operation, None)

    def _behaviour(self, operation: str) -> WrapperFunction | None:
        """The composed pipeline for one operation, or None when nothing
        is configured for it."""

        pipeline = self._pipelines.get(operation)
        terminal = self._terminals.get(operation)

        if not pipeline and terminal is None:
            return None

        composed = self._composed.get(operation)

        if composed is None:
            composed = _compose(pipeline or (), terminal)
            self._composed[operation] = composed

        return composed


class BindingGroup:
    """Several bindings applied and removed as a unit.

    Bindings are reachable by attribute or item access using the names
    they were given. apply() rolls back on partial failure; remove()
    removes in reverse order of application.
    """

    def __init__(self, points: dict[str, tuple[Any, str]]) -> None:
        self._bindings = {
            key: Binding(target, name, label=key)
            for key, (target, name) in points.items()
        }

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
    capture: CapturePolicy | None = None,
    capture_args: CapturePolicy | None = None,
    capture_result: CapturePolicy | None = None,
) -> Binding:
    """Create a binding for one target attribute.

    `target` is a module, class, instance, or a string naming a module.
    `name` is a dotted path to the attribute.

    The mode, 'callable' or 'attribute', is detected from whatever is at
    the target and selects which behaviour namespaces exist. Pass `mode=`
    to override for the ambiguous case of a callable stored as data.

    `missing_ok=True` permits binding a name that is not on the class,
    typically one assigned in __init__. Without it such a name raises
    AttributeError, because it is indistinguishable from a typo.

    `capture=` overrides how much of the recorded values this binding
    stores (a level such as SUMMARY, or a fn(name, value) callable),
    with `capture_args=` and `capture_result=` controlling the two axes
    separately and winning over the shorthand. Left unset, the binding
    follows what the sink consuming the events declares.

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
    )


def bindings(**points: tuple[Any, str]) -> BindingGroup:
    """Create several bindings at once, named by keyword.

    with bindings(charge=(Gateway, "charge"),
                  ledger=(Ledger, "record")) as group:
        ...
        group.charge.suspend()
    """

    return BindingGroup(dict(points))
