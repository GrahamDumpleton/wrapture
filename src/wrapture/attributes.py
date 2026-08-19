"""The descriptor behind attribute-mode bindings.

An attribute binding installs a data descriptor on the class, wrapping
whatever previously occupied the class attribute: another descriptor such
as a property, a plain class default, or wrapt's MISSING sentinel when
nothing was defined. Reads, writes and deletes hook the binding's
behaviour first and then perform the real operation, honouring a prior
descriptor's own logic beneath the interception.

The descriptor derives from wrapt's BaseObjectProxy and wraps the prior
definition, so wrapt's unwrap_object() can traverse and splice the
wrapper chain, and two attribute bindings on one name compose rather
than clobber. The read precedence and the write and delete delegation
mirror wrapt's own AttributeWrapper, which only hooks reads.

Inside a timeline, each operation additionally records an event of kind
"get", "set" or "delete" onto the ambient tape, mirroring the callable
mode's recording path.
"""

from __future__ import annotations

import inspect
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import wrapt
from wrapt import MISSING, BaseObjectProxy, apply_patch

from .capture import _capture_value, _level_of
from .events import Event, EventKind
from .exceptions import NotImplementedYetError
from .sinks import (
    _active_sinks,
    _in_recorder,
    _notify_error,
    _notify_exit,
    _record_event,
    _required_policy,
)
from .stacks import _capture as _capture_stack
from .timeline import (
    _capture_result,
    _pop,
    _push,
    _timelines_active,
)

if TYPE_CHECKING:
    from .bindings import Binding


def _read(prior: Any, attribute: str, instance: Any, owner: Any) -> Any:
    """Perform the real read, as the unwrapped attribute would.

    Standard lookup precedence applies: a data descriptor prior takes
    precedence over the instance dictionary, a non-data descriptor prior
    yields to it, and a plain class default is the fallback when no
    instance value exists. Only when the prior is the MISSING sentinel,
    meaning no definition of any sort existed, is AttributeError raised.
    """

    prior_type = type(prior)

    if hasattr(prior_type, "__get__") and (
        hasattr(prior_type, "__set__") or hasattr(prior_type, "__delete__")
    ):
        return prior.__get__(instance, owner)

    if attribute in instance.__dict__:
        return instance.__dict__[attribute]

    if hasattr(prior_type, "__get__"):
        return prior.__get__(instance, owner)

    if prior is not MISSING:
        return prior

    raise AttributeError(
        f"{type(instance).__name__!r} object has no attribute {attribute!r}"
    )


def _write(prior: Any, attribute: str, instance: Any, value: Any) -> None:
    """Perform the real write, delegating to a prior descriptor which
    implements __set__ so its validation and storage are honoured, and
    otherwise storing into the instance dictionary."""

    if hasattr(type(prior), "__set__"):
        prior.__set__(instance, value)
    else:
        instance.__dict__[attribute] = value


def _delete(prior: Any, attribute: str, instance: Any) -> None:
    """Perform the real delete, delegating to a prior descriptor which
    implements __delete__, and otherwise removing from the instance
    dictionary, raising AttributeError rather than KeyError when there is
    nothing to remove."""

    if hasattr(type(prior), "__delete__"):
        prior.__delete__(instance)
        return

    try:
        del instance.__dict__[attribute]
    except KeyError:
        raise AttributeError(
            f"{type(instance).__name__!r} object has no attribute {attribute!r}"
        ) from None


def _record(
    binding: Binding,
    kind: EventKind,
    instance: Any,
    attribute: str,
    operate: Callable[[], Any],
    value: Any = MISSING,
) -> Any:
    """Run one attribute operation, recording it onto the ambient tape.

    Mirrors the callable-mode recording path: no ambient tape (or the
    recorder's own reentrancy guard) means the operation just runs, and
    otherwise an event is recorded around it, with the operation pushed
    on the in-progress stack so anything it triggers nests under it.
    """

    # when=False is a behaviour-only binding: it never records, counts
    # nothing, and takes no part in gap detection.

    if binding._when is False:
        return operate()

    active = _active_sinks()
    if not active or _in_recorder.get():
        if not active and not _in_recorder.get() and _timelines_active():
            binding._note_missed_call()

        return operate()

    # The per-access predicate, mapped onto call shape the same way
    # behaviour stages are: a set passes the written value as the one
    # positional argument, a get or delete passes empty args.

    if binding._when is not None:
        call_args = (value,) if value is not MISSING else ()

        guard = _in_recorder.set(True)
        try:
            wanted = binding._when(instance, call_args, {})
        finally:
            _in_recorder.reset(guard)

        if not wanted:
            binding._filtered_calls += 1
            return operate()

    # The written value and the prior value are inbound data, so they
    # capture on the arguments axis, under the attribute's name so a
    # by-name policy such as redact() applies to writes too.

    policy = binding._capture_args
    if policy is None:
        policy = _required_policy(active, "capture_args")

    guard = _in_recorder.set(True)
    try:
        event = Event(
            kind,
            binding._path,
            label=binding._label,
            instance=instance,
            binding=binding,
            capture=_level_of(policy),
            injected=binding._injected(kind),
        )

        if binding._stack_depth is not None:
            event.stack = _capture_stack(binding._stack_depth)

        if value is not MISSING:
            event.value = _capture_value(policy, attribute, value)

        # The prior value, when cheaply available: only what already
        # sits in the instance dictionary. A prior held by a descriptor
        # would take running user code to read, so it is not recorded.

        if kind in ("set", "delete"):
            previous = getattr(instance, "__dict__", {}).get(attribute, MISSING)
            if previous is not MISSING:
                event.previous = _capture_value(policy, attribute, previous)

    finally:
        _in_recorder.reset(guard)

    # Position before delivery: pushed first, so sinks hearing
    # on_enter see the event's final depth and parent link. Timing
    # starts after the bookkeeping, so its overhead is not charged to
    # the observed operation.

    token = _push(event)
    _record_event(event, active)

    started = time.perf_counter()
    event.started = started

    try:
        outcome = operate()
    except BaseException as exc:
        event.duration = time.perf_counter() - started
        event.exception = exc
        _notify_error(event, active)
        raise
    finally:
        _pop(token)

    event.duration = time.perf_counter() - started

    # The value a read produced is its outcome, so it captures on the
    # result axis, exactly as a call's return value does.

    if kind == "get":
        result_policy = binding._capture_result
        if result_policy is None:
            result_policy = _required_policy(active, "capture_result")
        _capture_result(event, outcome, result_policy)

    _notify_exit(event, active)
    return outcome


class AttributeDescriptor(BaseObjectProxy[Any]):
    """The data descriptor an attribute binding installs on the class.

    Wraps the prior definition of the attribute, or MISSING when there
    was none. Each operation consults the owning binding: suspended means
    the operation passes straight through, and otherwise the binding's
    behaviour pipeline for the operation runs around the real operation.
    """

    def __init__(self, prior: Any, attribute: str, binding: Binding) -> None:
        super().__init__(prior)
        self._self_attribute = attribute
        self._self_wrapture_binding = binding

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        # Class-level access returns the descriptor itself. Being a
        # transparent proxy, introspection of the prior definition then
        # works through delegation.

        if instance is None:
            return self

        binding = self._self_wrapture_binding
        prior = self.__wrapped__
        attribute = self._self_attribute

        if binding._suspended:
            binding._suspended_calls += 1
            return _read(prior, attribute, instance, owner)

        behaviour = binding._behaviour("get")

        def read() -> Any:
            return _read(prior, attribute, instance, owner)

        def operate() -> Any:
            if behaviour is None:
                return read()
            return behaviour(read, instance, (), {})

        return _record(binding, "get", instance, attribute, operate)

    def __set__(self, instance: Any, value: Any) -> None:
        binding = self._self_wrapture_binding
        prior = self.__wrapped__
        attribute = self._self_attribute

        if binding._suspended:
            binding._suspended_calls += 1
            _write(prior, attribute, instance, value)
            return

        behaviour = binding._behaviour("set")

        def write(new_value: Any) -> None:
            _write(prior, attribute, instance, new_value)

        def operate() -> Any:
            if behaviour is None:
                write(value)
                return None
            return behaviour(write, instance, (value,), {})

        _record(binding, "set", instance, attribute, operate, value=value)

    def __delete__(self, instance: Any) -> None:
        binding = self._self_wrapture_binding
        prior = self.__wrapped__
        attribute = self._self_attribute

        if binding._suspended:
            binding._suspended_calls += 1
            _delete(prior, attribute, instance)
            return

        behaviour = binding._behaviour("delete")

        def erase() -> None:
            _delete(prior, attribute, instance)

        def operate() -> Any:
            if behaviour is None:
                erase()
                return None
            return behaviour(erase, instance, (), {})

        _record(binding, "delete", instance, attribute, operate)


def _resolve_parent(target: Any, name: str) -> tuple[Any, str]:
    """Resolve the object holding the final attribute of a dotted name."""

    if "." in name:
        prefix, attribute = name.rsplit(".", 1)
        parent = wrapt.resolve_path(target, prefix)[2]
        return parent, attribute

    if isinstance(target, str):
        __import__(target)
        return sys.modules[target], name

    return target, name


def install(binding: Binding, target: Any, name: str) -> AttributeDescriptor:
    """Install an AttributeDescriptor for a binding, returning the handle.

    The descriptor is installed on the class the name resolves to, with
    the prior definition found through the MRO so an inherited default
    keeps working beneath the interception. Whether installation created
    the attribute slot is recorded on the descriptor the same way
    wrapt.wrap_object() records it, so wrapt.unwrap_object() removes the
    slot rather than leaving a shadowing copy where appropriate.
    """

    parent, attribute = _resolve_parent(target, name)

    if inspect.ismodule(parent):
        raise NotImplementedYetError(
            f"{binding._label}: attribute bindings on a module are not"
            f" supported; module attribute access does not go through"
            f" class descriptors"
        )

    if not inspect.isclass(parent):
        raise TypeError(
            f"{binding._label}: an attribute binding installs a descriptor"
            f" on the class, so the target must be a class, not an instance"
        )

    prior: Any = MISSING
    for cls in inspect.getmro(parent):
        if attribute in vars(cls):
            prior = vars(cls)[attribute]
            break

    # An absent attribute is only bindable when the binding was created
    # with missing_ok=True; with detection skipped by an explicit mode=,
    # this is where a misspelled name surfaces.

    if prior is MISSING and not binding._missing_ok:
        raise AttributeError(
            f"{binding._label}: attribute {attribute!r} is not defined on"
            f" {parent.__name__!r}; pass missing_ok=True to bind a name"
            f" that is assigned only on instances"
        )

    created = attribute not in vars(parent)

    descriptor = AttributeDescriptor(prior, attribute, binding)
    descriptor.__self_setattr__("__wrapt_wrap_object_created_slot__", created)
    apply_patch(parent, attribute, descriptor)

    return descriptor
