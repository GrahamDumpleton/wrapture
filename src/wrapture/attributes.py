"""The descriptor behind attribute-mode bindings.

An attribute binding installs a data descriptor on the class, wrapping
whatever previously occupied the class attribute: another descriptor such
as a property, a plain class default, or wrapt's MISSING sentinel when
nothing was defined. Reads, writes and deletes hook the binding's
behaviour first and then perform the real operation, honouring a prior
descriptor's own logic beneath the interception.

A module owner works the same way one level up: the descriptor goes on
a per-module subclass of the module's type, assigned to the module's
`__class__` while any binding on the module is applied, and the value
itself stays in the module's `__dict__`. One such class serves every
binding on the module, and it is removed again when the last binding
is.

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
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import wrapt
from wrapt import MISSING, BaseObjectProxy, apply_patch

from .capture import REFERENCE, CapturePolicy, _capture_value, _level_of
from .events import Event, EventKind
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
    _SILENCE_ALL,
    _SILENCE_SPANS,
    _capture_result,
    _hide,
    _pop,
    _push,
    _silence,
    _suppressed,
    _timelines_active,
    _unhide,
)

if TYPE_CHECKING:
    from .behaviours import Phase
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

    # A module may supply names it does not define through a module-level
    # __getattr__ (PEP 562); a missing_ok binding on such a name defers
    # to it rather than hiding it.

    if inspect.ismodule(instance):
        fallback = instance.__dict__.get("__getattr__")
        if fallback is not None:
            return fallback(attribute)

        raise AttributeError(
            f"module {instance.__name__!r} has no attribute {attribute!r}"
        )

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


def _build_event(
    binding: Binding,
    kind: EventKind,
    instance: Any,
    attribute: str,
    policy: CapturePolicy,
    value: Any,
    phase: Phase | None,
    *,
    recording: bool = False,
) -> Event:
    """Construct the event for one attribute operation, under the
    recorder guard so capture that runs user code does not record.

    The declarations are resolved on the call shape the when=
    predicate sees for the access (the written value as the one
    positional argument on a set, empty args otherwise), only for a
    recorded access; a private event built for a watched phase carries
    the static ones."""

    guard = _in_recorder.set(True)
    try:
        call_args = (value,) if value is not MISSING else ()
        label, category, data = binding._declared(instance, call_args, {}, recording)

        event = Event(
            kind,
            binding._path,
            label=label,
            category=category,
            instance=instance,
            binding=binding,
            capture=_level_of(policy),
            injected=phase is not None and phase.injected,
            phase=binding._phase_of(phase),
        )

        if data:
            event.data.update(data)

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

    return event


def _silenced(
    binding: Binding,
    kind: EventKind,
    instance: Any,
    attribute: str,
    operate: Callable[[Event | None], Any],
    value: Any,
    slot: list[Phase | None],
) -> Any:
    """Run an unrecorded attribute operation with recording suppressed
    beneath it for its extent, so nothing it triggers records either."""

    token = _silence(_SILENCE_ALL)
    try:
        return _quiet(binding, kind, instance, attribute, operate, value, slot)
    finally:
        _suppressed.reset(token)


def _quiet(
    binding: Binding,
    kind: EventKind,
    instance: Any,
    attribute: str,
    operate: Callable[[Event | None], Any],
    value: Any,
    slot: list[Phase | None],
) -> Any:
    """Run an unrecorded attribute operation, still showing it to a
    phase with an until= exit through a private event, as the callable
    path does."""

    try:
        outcome = operate(None)
    except BaseException as exc:
        phase = slot[0]

        if phase is not None and phase.watches:
            event = _build_event(
                binding, kind, instance, attribute, REFERENCE, value, phase
            )
            event.exception = exc
            binding._completed(kind, phase, event)

        raise

    phase = slot[0]

    if phase is not None and phase.watches:
        event = _build_event(
            binding, kind, instance, attribute, REFERENCE, value, phase
        )
        if kind == "get":
            event.result = outcome
        binding._completed(kind, phase, event)

    return outcome


def _record(
    binding: Binding,
    kind: EventKind,
    instance: Any,
    attribute: str,
    operate: Callable[[Event | None], Any],
    value: Any = MISSING,
    slot: list[Phase | None] | None = None,
) -> Any:
    """Run one attribute operation, recording it onto the ambient tape.

    Mirrors the callable-mode recording path: no ambient tape (or the
    recorder's own reentrancy guard) means the operation just runs, and
    otherwise an event is recorded around it, with the operation pushed
    on the in-progress stack so anything it triggers nests under it.
    `slot` holds the phase handling the operation, updated by dispatch
    if a sequence hands over mid-operation.
    """

    if slot is None:
        slot = [None]

    # when=False is a behaviour-only binding: it never records, counts
    # nothing, and takes no part in gap detection. With tree=True it
    # also silences everything beneath the access while recording.

    if binding._when is False:
        if binding._tree and _active_sinks():
            return _silenced(binding, kind, instance, attribute, operate, value, slot)

        return _quiet(binding, kind, instance, attribute, operate, value, slot)

    active = _active_sinks()
    if not active or _in_recorder.get():
        if not active and not _in_recorder.get() and _timelines_active():
            binding._note_missed_call()

        return _quiet(binding, kind, instance, attribute, operate, value, slot)

    # Beneath a leaf, or an operation a tree=True binding declined,
    # nothing records, the predicate included; only a tree decline's
    # silence is counted, a leaf's being visible on the tape itself.

    silenced = _suppressed.get()
    if silenced:
        if silenced >= _SILENCE_ALL:
            binding._filtered_calls += 1

        hidden = _hide()
        try:
            return _quiet(binding, kind, instance, attribute, operate, value, slot)
        finally:
            _unhide(hidden)

    # The per-access predicate, mapped onto call shape the same way
    # behaviour stages are: a set passes the written value as the one
    # positional argument, a get or delete passes empty args. A
    # decline with tree=True silences everything beneath the access.

    if binding._when is not None:
        call_args = (value,) if value is not MISSING else ()

        guard = _in_recorder.set(True)
        try:
            wanted = binding._when(instance, call_args, {})
        finally:
            _in_recorder.reset(guard)

        if not wanted:
            binding._filtered_calls += 1

            # A per-access decline hides the access from current_event()
            # for its extent, as a declined call is hidden.

            hidden = _hide()
            try:
                if binding._tree:
                    return _silenced(
                        binding, kind, instance, attribute, operate, value, slot
                    )

                return _quiet(binding, kind, instance, attribute, operate, value, slot)
            finally:
                _unhide(hidden)

    # The written value and the prior value are inbound data, so they
    # capture on the arguments axis, under the attribute's name so a
    # by-name policy such as redact() applies to writes too. An until=
    # predicate needs the value whatever the sinks asked for.

    phase = slot[0]
    watching = phase is not None and phase.watches

    policy = binding._capture_args
    if policy is None:
        policy = _required_policy(active, "capture_args")
    if watching and _level_of(policy) < REFERENCE:
        policy = REFERENCE

    event = _build_event(
        binding, kind, instance, attribute, policy, value, phase, recording=True
    )

    # Position before delivery: pushed first, so sinks hearing
    # on_enter see the event's final depth and parent link. Timing
    # starts after the bookkeeping, so its overhead is not charged to
    # the observed operation.

    token = _push(event)
    _record_event(event, active)

    started = time.perf_counter()
    event.started = started

    # A leaf silences the spans beneath it for the operation.

    silence = _silence(_SILENCE_SPANS) if binding._leaf else None

    try:
        outcome = operate(event)
    except BaseException as exc:
        event.duration = time.perf_counter() - started
        event.exception = exc
        _notify_error(event, active)
        binding._completed(kind, slot[0], event)
        raise
    finally:
        if silence is not None:
            _suppressed.reset(silence)
        _pop(token)

    event.duration = time.perf_counter() - started

    # The value a read produced is its outcome, so it captures on the
    # result axis, exactly as a call's return value does.

    if kind == "get":
        result_policy = binding._capture_result
        if result_policy is None:
            result_policy = _required_policy(active, "capture_result")
        if watching and _level_of(result_policy) < REFERENCE:
            result_policy = REFERENCE
        _capture_result(event, outcome, result_policy)

    _notify_exit(event, active)
    binding._completed(kind, slot[0], event)
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

        phase = binding._select("get")
        slot: list[Phase | None] = [phase]

        def read() -> Any:
            return _read(prior, attribute, instance, owner)

        def operate(event: Event | None) -> Any:
            return binding._invoke(
                "get", phase, read, instance, (), {}, event, slot=slot
            )

        return _record(binding, "get", instance, attribute, operate, slot=slot)

    def __set__(self, instance: Any, value: Any) -> None:
        binding = self._self_wrapture_binding
        prior = self.__wrapped__
        attribute = self._self_attribute

        if binding._suspended:
            binding._suspended_calls += 1
            _write(prior, attribute, instance, value)
            return

        phase = binding._select("set")
        slot: list[Phase | None] = [phase]

        def write(new_value: Any) -> None:
            _write(prior, attribute, instance, new_value)

        def operate(event: Event | None) -> Any:
            return binding._invoke(
                "set", phase, write, instance, (value,), {}, event, slot=slot
            )

        _record(binding, "set", instance, attribute, operate, value=value, slot=slot)

    def __delete__(self, instance: Any) -> None:
        binding = self._self_wrapture_binding
        prior = self.__wrapped__
        attribute = self._self_attribute

        if binding._suspended:
            binding._suspended_calls += 1
            _delete(prior, attribute, instance)
            return

        phase = binding._select("delete")
        slot: list[Phase | None] = [phase]

        def erase() -> None:
            _delete(prior, attribute, instance)

        def operate(event: Event | None) -> Any:
            return binding._invoke(
                "delete", phase, erase, instance, (), {}, event, slot=slot
            )

        _record(binding, "delete", instance, attribute, operate, slot=slot)


_MODULE_BASE = "_wrapture_module_base"

_module_lock = threading.Lock()


def _module_class(module: Any) -> type | None:
    """The per-module class wrapture assigned to a module, or None when
    the module's type is not one of ours."""

    cls = type(module)
    if _MODULE_BASE in vars(cls):
        return cls

    return None


def _intercept_module(module: Any, binding: Binding) -> type:
    """Find or create the per-module class that carries descriptors for
    a module, assigning it to the module's __class__ on creation.

    The class derives from the module's current type rather than
    ModuleType, so anything that already replaced the module's class is
    kept working beneath the interception. It is named "module" so the
    type's name, and messages built from it, read the same as before.
    """

    existing = _module_class(module)
    if existing is not None:
        return existing

    # From CPython 3.14 (python/cpython#103951, PR #126264, still the
    # case on main) the LOAD_ATTR specialisation for modules is chosen on
    # the type's getattro slot alone, anything sharing ModuleType's, and
    # then reads the module dictionary directly, skipping data
    # descriptors on the type. Before 3.14 the guard was an exact type
    # check, so subclasses always took the general path and descriptors
    # worked. The change was made for speed and neither the issue nor
    # the PR mentions descriptors, so the loss looks unintended, an
    # unreported regression rather than a decision. Defining
    # __getattribute__ gives the class its own slot, so every access
    # takes the general path and the descriptors are seen, on every
    # version, whether or not upstream changes again. It delegates
    # straight to the base, so messages and any module __getattr__
    # behave as before.

    base: Any = type(module)

    def __getattribute__(self: Any, name: str) -> Any:
        return base.__getattribute__(self, name)

    namespace = {
        _MODULE_BASE: base,
        "__module__": base.__module__,
        "__getattribute__": __getattribute__,
    }
    cls = type("module", (base,), namespace)

    try:
        module.__class__ = cls
    except TypeError as exc:
        raise TypeError(
            f"{binding._label}: attribute bindings on module"
            f" {module.__name__!r} are not possible because its type"
            f" {base.__name__!r} does not allow __class__ assignment"
            f" ({exc}). To hold a value in the attribute while applied,"
            f" use attr={binding._name.rsplit('.', 1)[-1]!r} instead"
        ) from None

    return cls


def _release_module(module: Any) -> None:
    """Put the module's original class back once no descriptor remains
    on the per-module class and it is still what the module uses."""

    cls = _module_class(module)
    if cls is None:
        return

    if any(isinstance(value, AttributeDescriptor) for value in vars(cls).values()):
        return

    module.__class__ = vars(cls)[_MODULE_BASE]


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


def _install_on_module(
    binding: Binding, module: Any, attribute: str
) -> AttributeDescriptor:
    """Install the descriptor for a module attribute on the per-module
    class, creating the class on first use."""

    # An absent attribute is only bindable with missing_ok=True, as for
    # a class owner. A name a module-level __getattr__ would supply
    # counts as absent: it is not in the module's dictionary.

    if attribute not in vars(module) and not binding._missing_ok:
        raise AttributeError(
            f"{binding._label}: attribute {attribute!r} is not defined on"
            f" module {module.__name__!r}; pass missing_ok=True to bind a"
            f" name that is assigned later"
        )

    with _module_lock:
        cls = _intercept_module(module, binding)

        prior: Any = vars(cls).get(attribute, MISSING)
        created = prior is MISSING

        descriptor = AttributeDescriptor(prior, attribute, binding)
        descriptor.__self_setattr__("__wrapt_wrap_object_created_slot__", created)
        apply_patch(cls, attribute, descriptor)

    return descriptor


def install(binding: Binding, target: Any, name: str) -> AttributeDescriptor:
    """Install an AttributeDescriptor for a binding, returning the handle.

    The descriptor is installed on the class the name resolves to, with
    the prior definition found through the MRO so an inherited default
    keeps working beneath the interception. Whether installation created
    the attribute slot is recorded on the descriptor the same way
    wrapt.wrap_object() records it, so wrapt.unwrap_object() removes the
    slot rather than leaving a shadowing copy where appropriate. A
    module owner installs on the per-module class instead.
    """

    parent, attribute = _resolve_parent(target, name)

    if inspect.ismodule(parent):
        return _install_on_module(binding, parent, attribute)

    if not inspect.isclass(parent):
        raise TypeError(
            f"{binding._label}: an attribute binding installs a descriptor"
            f" on the class, so the target must be a class, not an instance."
            f" To observe reads on every instance, bind the class; to set"
            f" the value on this one object, use attr={attribute!r}"
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


def _holder(target: Any, name: str, descriptor: AttributeDescriptor) -> tuple[Any, str]:
    """The object whose namespace holds a binding's descriptor, and the
    attribute name there: for a module owner the per-module class in
    its MRO that has the descriptor, otherwise the owner itself. Raises
    when the owner no longer resolves or no class holds it."""

    parent, attribute = _resolve_parent(target, name)

    if not inspect.ismodule(parent):
        return parent, attribute

    # Something may have layered its own subclass above ours after the
    # install, so search the MRO rather than insisting on type(module).

    for cls in type(parent).__mro__:
        if _MODULE_BASE not in vars(cls):
            continue
        current = vars(cls).get(attribute, MISSING)
        if current is not MISSING and wrapt.is_wrapped_by(current, descriptor):
            return cls, attribute

    raise LookupError(f"module {parent.__name__!r} does not hold {attribute!r}")


def is_installed(target: Any, name: str, descriptor: AttributeDescriptor) -> bool:
    """Whether a binding's descriptor is still in place at its location,
    alone or within a chain of composed descriptors."""

    try:
        holder, attribute = _holder(target, name, descriptor)
        current = wrapt.resolve_path(holder, attribute)[2]
    except Exception:
        return False

    return bool(wrapt.is_wrapped_by(current, descriptor))


def uninstall(
    target: Any,
    name: str,
    descriptor: AttributeDescriptor,
    *,
    missing_ok: bool,
) -> None:
    """Remove a binding's descriptor, splicing it out of a composed
    chain where needed, and for a module owner release the per-module
    class once nothing else is installed on it."""

    parent, attribute = _resolve_parent(target, name)

    if not inspect.ismodule(parent):
        wrapt.unwrap_object(parent, attribute, descriptor, missing_ok=missing_ok)
        return

    with _module_lock:
        try:
            cls, attribute = _holder(target, name, descriptor)
        except LookupError:
            if missing_ok:
                return
            raise ValueError(
                f"module {parent.__name__!r} no longer holds the descriptor"
                f" for {attribute!r}, so it cannot be removed"
            ) from None

        wrapt.unwrap_object(cls, attribute, descriptor, missing_ok=missing_ok)
        _release_module(parent)
