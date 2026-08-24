"""Observation for callables that live nowhere a binding can point.

A binding names a location that can be resolved, patched and restored:
an attribute, or a mapping entry named with item=. Plenty of callables
never sit at one long enough to be named: closures, partials, work put
on a queue, callbacks handed over and kept somewhere private, view
functions on their way into a framework's registration. observed()
wraps the value instead of the location: it returns a transparent proxy
that records a "call" event per invocation, and the caller places it
wherever the original was going.

The division of responsibility is the inverse of a binding's. A
binding owns installation and removal; observed() owns neither, so
there is no apply() or remove(), and putting the original back is the
caller's job, exactly as placing the wrapper was. Everything else
about a binding's character is kept: suspend() and resume(), the
honest counters, participation in timelines with events nesting as
usual, and the events property for assertions.

The proxy is deliberately transparent: __name__, __doc__, __code__,
signature introspection and equality all delegate to the wrapped
callable, so registries that inspect what they are handed (a
framework deriving an endpoint name, ensure_sync detecting a
coroutine function, a duplicate-registration check comparing
functions) behave as if the wrapper were not there. Placed on a
class, the proxy binds as a method exactly as the wrapped function
would: calls made through instances record the instance, and the
bound signature drops self.

observed() is observation only: there are no behaviour namespaces, so
it cannot stub or fail-inject. Interventions want a removable home,
and a free-floating callable has none; use binding() for those.
"""

from __future__ import annotations

import inspect
import time
import warnings
from collections.abc import Callable
from typing import Any, cast, overload

from wrapt import BoundFunctionWrapper, FunctionWrapper, wrapper_chain

from ._wrappermixins import (
    BoundConventionOverrideMixin,
    BoundSignatureOverrideMixin,
    ConventionOverrideMixin,
    SignatureOverrideMixin,
)
from .behaviours import _named_after
from .bindings import (
    _record_async_generator,
    _record_awaited,
    _record_generator,
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
from .events import Event, normalized_arguments, var_keyword_name
from .exceptions import RecordingGapWarning
from .sinks import (
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


def _describe(fn: Any) -> str:
    # Derive the path from the callable itself, the same
    # module:qualname convention bindings derive from their target. No
    # label is derived: an unnamed observation records label None and
    # every consumer falls back to the path.

    module = getattr(fn, "__module__", None) or "observed"
    qualname = (
        getattr(fn, "__qualname__", None)
        or getattr(type(fn), "__qualname__", None)
        or "callable"
    )

    return f"{module}:{qualname}"


class BoundObservedCallable(
    BoundConventionOverrideMixin,
    BoundSignatureOverrideMixin,
    BoundFunctionWrapper[Any, Any],
):
    """The bound form of an ObservedCallable placed on a class.

    Produced by attribute access, exactly as a function becomes a bound
    method; recording state, events and lifecycle all live on and
    delegate to the parent proxy.
    """


class ObservedCallable(
    ConventionOverrideMixin,
    SignatureOverrideMixin,
    FunctionWrapper[Any, Any],
):
    """The recording proxy observed() returns.

    Calling it records one "call" event inside a recording scope and
    otherwise calls straight through. Placed on a class it binds as a
    method, recording the instance calls are made on. Not created
    directly; see observed().
    """

    __bound_function_wrapper__ = BoundObservedCallable

    def __init__(
        self,
        wrapped: Callable[..., Any],
        *,
        path: str,
        label: str | None,
        capture_args: CapturePolicy | None,
        capture_result: CapturePolicy | None,
        stack: int | None,
        when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any] | bool | None,
        signature: Any = None,
        convention: str | None = None,
        precheck: Callable[[Callable[..., Any], tuple[Any, ...], dict[str, Any]], None]
        | None = None,
    ) -> None:
        # The wrapper function hands every call, bound or not, back to
        # the proxy with the target and instance wrapt resolved.

        def invoke(
            target: Callable[..., Any],
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return self._invoke(target, instance, args, kwargs)

        super().__init__(wrapped, invoke, signature=signature, convention=convention)

        self._self_path = path
        self._self_label = label
        self._self_display = label or path
        self._self_precheck = precheck
        self._self_capture_args = capture_args
        self._self_capture_result = capture_result
        self._self_stack_depth = stack
        self._self_when = when

        self._self_suspended = False
        self._self_suspended_calls = 0
        self._self_filtered_calls = 0
        self._self_missed_calls = 0
        self._self_gap_warned = False

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._self_display!r}>"

    # -- identity ----------------------------------------------------------

    @property
    def path(self) -> str:
        """The derived module:qualname location of the wrapped callable."""

        return self._self_path

    @property
    def label(self) -> str | None:
        """The assigned display name, or None when none was given, in
        which case events fall back to the path."""

        return self._self_label

    # -- lifecycle ---------------------------------------------------------

    @property
    def suspended(self) -> bool:
        """Whether the proxy is currently inert."""

        return self._self_suspended

    def suspend(self) -> ObservedCallable:
        """Make the proxy inert without unwrapping it. Returns self."""

        self._self_suspended = True
        return self

    def resume(self) -> ObservedCallable:
        """Reactivate a suspended proxy. Returns self."""

        self._self_suspended = False
        return self

    @property
    def suspended_calls(self) -> int:
        """Calls that arrived while the proxy was suspended."""

        return self._self_suspended_calls

    @property
    def filtered_calls(self) -> int:
        """Calls the when= predicate declined to record."""

        return self._self_filtered_calls

    @property
    def missed_calls(self) -> int:
        """Calls that ran with no recording context while a timeline was
        active elsewhere, typically on a thread."""

        return self._self_missed_calls

    @property
    def events(self) -> EventLog:
        """This proxy's events from the enclosing timeline.

        Raises RuntimeError outside a timeline, so "recorded nothing"
        can never be mistaken for "not recording".
        """

        tape = _current_tape()
        if tape is None:
            raise RuntimeError(
                f"{self._self_display}: events are only recorded inside a timeline()"
            )

        return tape.for_binding(self)

    # -- the call ----------------------------------------------------------

    def _note_missed_call(self) -> None:
        self._self_missed_calls += 1

        if not self._self_gap_warned:
            self._self_gap_warned = True
            warnings.warn(
                f"{self._self_display}: an observed call ran on a thread with"
                f" no recording context while a timeline was active"
                f" elsewhere, so it was not recorded. To record work on"
                f" this thread, wrap its target with wrapture.propagate(...)."
                f" Misses are counted on missed_calls.",
                RecordingGapWarning,
                stacklevel=3,
            )

    def _record_call(
        self,
        active: tuple[Any, ...],
        target: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Event:
        # Mirrors the callable binding's argument capture: NONE skips
        # signature binding entirely; at REFERENCE the raw call shape
        # rides along; above it, capture through the normalized form.
        # The target is what wrapt resolved for this call, the bound
        # method when the proxy sits on a class, so normalization never
        # sees self.

        policy = self._self_capture_args
        if policy is None:
            policy = _required_policy(active, "capture_args")
        level = _level_of(policy)

        event = Event(
            "call",
            self._self_path,
            label=self._self_label,
            instance=instance,
            binding=self,
            capture=level,
        )

        if self._self_stack_depth is not None:
            event.stack = _capture_stack(self._self_stack_depth)

        if level > NONE:
            arguments = normalized_arguments(target, args, kwargs)
            if arguments is not None:
                event.var_keyword = var_keyword_name(target)

            if not callable(policy) and level == REFERENCE:
                event.args = args
                event.kwargs = kwargs
                event.arguments = arguments
            elif arguments is not None:
                event.arguments = {
                    name: _capture_value(policy, name, value)
                    for name, value in arguments.items()
                }
            else:
                event.args = tuple(
                    _capture_value(policy, None, value) for value in args
                )
                event.kwargs = {
                    name: _capture_value(policy, name, value)
                    for name, value in kwargs.items()
                }

        return event

    def _invoke(
        self,
        target: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        # A precheck (stub() strictness) rejects a call that does not
        # fit before it is counted, recorded, or answered, suspended or
        # not, exactly as a strict binding's signature check does.

        precheck = self._self_precheck
        if precheck is not None:
            precheck(target, args, kwargs)

        if self._self_suspended:
            self._self_suspended_calls += 1
            return target(*args, **kwargs)

        # when=False never records, counts nothing, and takes no part
        # in gap detection.

        if self._self_when is False:
            return target(*args, **kwargs)

        active = _active_sinks()

        if not active or _in_recorder.get():
            if not active and not _in_recorder.get() and _timelines_active():
                self._note_missed_call()
            return target(*args, **kwargs)

        when = self._self_when
        if callable(when):
            guard = _in_recorder.set(True)
            try:
                wanted = when(instance, args, kwargs)
            finally:
                _in_recorder.reset(guard)

            if not wanted:
                self._self_filtered_calls += 1
                return target(*args, **kwargs)

        guard = _in_recorder.set(True)
        try:
            event = self._record_call(active, target, instance, args, kwargs)
        finally:
            _in_recorder.reset(guard)

        base = _stack.get()
        token = _push(event)
        _record_event(event, active)

        started = time.perf_counter()
        event.started = started

        try:
            outcome = target(*args, **kwargs)
        except BaseException as exc:
            event.duration = time.perf_counter() - started
            event.exception = exc
            _notify_error(event, active)
            raise
        finally:
            _pop(token)

        result_policy = self._self_capture_result
        if result_policy is None:
            result_policy = _required_policy(active, "capture_result")

        if inspect.isgenerator(outcome):
            return _record_generator(outcome, event, base, result_policy, active)

        if inspect.isasyncgen(outcome):
            return _named_after(
                _record_async_generator(outcome, event, base, result_policy, active),
                outcome,
            )

        if inspect.isawaitable(outcome):
            return _named_after(
                _record_awaited(outcome, event, base, result_policy, active), outcome
            )

        event.duration = time.perf_counter() - started
        _capture_result(event, outcome, result_policy)
        _notify_exit(event, active)
        return outcome


@overload
def observed(
    fn: Callable[..., Any],
    *,
    label: str | None = None,
    capture: CapturePolicy | str | None = None,
    capture_args: CapturePolicy | str | None = None,
    capture_result: CapturePolicy | str | None = None,
    stack: int | str | None = None,
    when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any] | bool | None = None,
) -> ObservedCallable: ...


@overload
def observed(
    fn: None = None,
    *,
    label: str | None = None,
    capture: CapturePolicy | str | None = None,
    capture_args: CapturePolicy | str | None = None,
    capture_result: CapturePolicy | str | None = None,
    stack: int | str | None = None,
    when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any] | bool | None = None,
) -> Callable[[Callable[..., Any]], ObservedCallable]: ...


def observed(
    fn: Callable[..., Any] | None = None,
    *,
    label: str | None = None,
    capture: CapturePolicy | str | None = None,
    capture_args: CapturePolicy | str | None = None,
    capture_result: CapturePolicy | str | None = None,
    stack: int | str | None = None,
    when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any] | bool | None = None,
) -> ObservedCallable | Callable[[Callable[..., Any]], ObservedCallable]:
    """Wrap a bare callable so its calls record, wherever it ends up.

    For callables no binding can reach because they live at no
    nameable location: closures, partials, thread targets, callbacks
    handed over and kept somewhere private, a value on its way into a
    registration call. (A callable that does sit in a mapping, a
    handler in a dispatch table, is reachable by a binding with item=
    and mode="callable", which also removes itself.) The returned proxy
    is transparent (name, signature, equality and introspection all
    delegate), records one "call" event per invocation inside a
    recording scope, and costs almost nothing when nothing listens.
    Place it wherever the original was going; putting the original
    back is equally the caller's job, which is the whole difference
    from a binding.

    Works as a decorator in both spellings: bare `@observed` directly
    on a def, and `@observed(label=..., capture=...)` when options are
    wanted, where the call with no callable returns the decorator that
    captures the function below it. Everything but the callable itself
    is keyword-only.

    The keyword options are the uniform subset binding() takes, with
    the same meanings. `when=` receives (instance, args, kwargs), the
    instance being None for a free-standing callable and the bound
    object when the proxy sits on a class, and accepts a boolean in
    place of the predicate as binding() does.

    The assigned label, or the derived module:qualname path when no
    label is given, identifies the observation, and that is what
    makes dynamic application safe. The callable's full wrapper chain
    is inspected with wrapt's wrapper_chain(), which sees through
    proxies and functools.wraps() decorators alike; an
    ObservedCallable layer already carrying the same identity means
    this observation is applied, however deeply a later wrapper
    buried it, and the callable is returned unchanged (exactly as
    given, with any such later wrappers intact), that layer's options
    standing. The derived path keeps the wrap-in-place idiom,
    registry[k] = observed(registry[k]), safe to run any number of
    times; but it reads introspection off the object handed in, which
    an interleaved third-party wrapper may fail to preserve, so
    wherever double wrapping is a real risk, give a pre-determined
    label. Distinct identities stack: each layer records its own
    event, one nested under the other. Stacking by accident cannot be
    told from intent, so it is not an error; it shows up honestly as
    double counting in the results.

    No label is derived from the callable: an unnamed observation
    records label None and every renderer falls back to the path, so
    a printed name with a colon in it is always the real
    module:qualname location.
    """

    # Called with only options, hand back the decorator that captures
    # the function defined below it: the standard optional-argument
    # decorator pattern.

    if fn is None:

        def apply(target: Callable[..., Any]) -> ObservedCallable:
            return observed(
                target,
                label=label,
                capture=capture,
                capture_args=capture_args,
                capture_result=capture_result,
                stack=stack,
                when=when,
            )

        return apply

    if not callable(fn):
        raise TypeError(f"observed() wraps a callable, got {fn!r}")

    depth = _resolve_depth(stack)
    if depth is not None and depth < 1:
        raise ValueError(
            f"stack must be None, 'caller', 'full' or a positive frame"
            f" count, got {stack!r}"
        )

    # As with wrapt's `enabled`, when= accepts a boolean as well as a
    # predicate: True is the always-record default, False never records.

    if when is True:
        when = None
    elif when is not False and when is not None and not callable(when):
        raise ValueError(
            f"when must be a boolean, a callable taking (instance, args,"
            f" kwargs), or None, got {when!r}"
        )

    path = _describe(fn)

    # Dedupe by identity across the full wrapper chain: the identity is
    # the assigned label, or the derived path when unnamed, so finding
    # it on an ObservedCallable layer anywhere in the chain means this
    # observation is already applied, and fn goes back exactly as
    # given. The isinstance check is precise even against proxy
    # __class__ transparency, since only a layer whose real type is
    # ObservedCallable matches.

    identity = label or path

    for layer in wrapper_chain(fn):
        if isinstance(layer, ObservedCallable) and (layer.label or layer.path) == (
            identity
        ):
            return cast(ObservedCallable, fn)

    return ObservedCallable(
        fn,
        path=path,
        label=label,
        capture_args=_resolve_policy(
            capture_args if capture_args is not None else capture
        ),
        capture_result=_resolve_policy(
            capture_result if capture_result is not None else capture
        ),
        stack=depth,
        when=when,
    )
