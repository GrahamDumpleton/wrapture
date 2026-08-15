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
"""

from __future__ import annotations

import inspect
import sys
from typing import TYPE_CHECKING, Any

import wrapt
from wrapt import MISSING, BaseObjectProxy, apply_patch

from .exceptions import NotImplementedYetError

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

        if behaviour is None:
            return _read(prior, attribute, instance, owner)

        def read() -> Any:
            return _read(prior, attribute, instance, owner)

        return behaviour(read, instance, (), {})

    def __set__(self, instance: Any, value: Any) -> None:
        binding = self._self_wrapture_binding
        prior = self.__wrapped__
        attribute = self._self_attribute

        if binding._suspended:
            binding._suspended_calls += 1
            _write(prior, attribute, instance, value)
            return

        behaviour = binding._behaviour("set")

        if behaviour is None:
            _write(prior, attribute, instance, value)
            return

        def write(new_value: Any) -> None:
            _write(prior, attribute, instance, new_value)

        behaviour(write, instance, (value,), {})

    def __delete__(self, instance: Any) -> None:
        binding = self._self_wrapture_binding
        prior = self.__wrapped__
        attribute = self._self_attribute

        if binding._suspended:
            binding._suspended_calls += 1
            _delete(prior, attribute, instance)
            return

        behaviour = binding._behaviour("delete")

        if behaviour is None:
            _delete(prior, attribute, instance)
            return

        def erase() -> None:
            _delete(prior, attribute, instance)

        behaviour(erase, instance, (), {})


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
