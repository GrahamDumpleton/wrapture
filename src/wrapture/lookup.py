"""Finding a binding you do not hold.

Every assertion surface hangs off the Binding object: `events`,
`assert_order()` steps, `is_wrapping()`, `suspend()`. A binding applied
by an instrumentation, by a config file, or by application code at
startup is never handed to the test that wants to assert on it, and a
callable decorated with `@observed` carries its recorder inside the
wrapper. The functions here recover the handle from what the test does
have: the location it knows (`find_binding`), or the wrapped object in
hand (`binding_of`).
"""

from __future__ import annotations

from typing import Any

from wrapt import MISSING, BoundFunctionWrapper, wrapper_chain

from .bindings import (
    Binding,
    _applied_bindings,
    _bare_path,
    _location,
    _slot_path,
)
from .exceptions import AmbiguousBindingError, NoBindingError
from .observed import ObservedCallable, _observed_callables

# What a lookup hands back: a binding, or an observed() proxy, which
# records and asserts the same way but owns no location.

Observer = Binding | ObservedCallable


def _display(observer: Observer) -> str:
    return observer.label or observer.path


def _order(observer: Observer) -> int:
    if isinstance(observer, ObservedCallable):
        return observer._self_sequence

    return observer._sequence


def _query_path(
    target: Any, attrs: tuple[str, ...], attr: str | None, item: Any
) -> str:
    # The same normalisation binding() applies, so a lookup spelled the
    # way the binding was created finds it, whichever of the equivalent
    # spellings each used. Nothing is imported or resolved: a string
    # target is taken as the module name it will have been recorded as.

    slot = attr is not None or item is not MISSING
    target, name = _location(target, attrs, allow_bare=True)

    if slot:
        kind = "attr" if attr is not None else "item"
        return _slot_path(target, name, kind, attr if attr is not None else item)

    return _bare_path(target, name)


def find_bindings(
    target: Any = None,
    *attrs: str,
    label: str | None = None,
    attr: str | None = None,
    item: Any = MISSING,
) -> list[Observer]:
    """Every applied binding, and every live observed() proxy, at a
    location and/or with a label.

    The location is spelled as binding() takes it: a module, class or
    instance plus attribute steps, or a "module:path" string, with
    `attr=` or `item=` for a slot binding. It is matched against each
    binding's `path`, which is derived from the target and unaffected
    by any label, so the query names the place, not the name someone
    gave it. `label=` is matched against the name a binding shows in
    output: its assigned label, or its path when it has none. Given
    together they both have to match; one of them is required.

    Only bindings currently applied are found, so a binding whose scope
    has ended is gone from the results rather than returned stale;
    suspended ones are included. The result is in order of application,
    earliest first, so on a target with stacked bindings the last entry
    is the outermost layer. It is the real object in each case: changes
    made to it are seen by whoever applied it.
    """

    if target is None and label is None:
        raise ValueError("find_bindings() needs a location, a label, or both")

    if attr is not None and item is not MISSING:
        raise TypeError("find_bindings() takes attr= or item=, not both")

    if target is None and (attrs or attr is not None or item is not MISSING):
        raise TypeError("find_bindings() needs a target to go with the attribute path")

    path = _query_path(target, attrs, attr, item) if target is not None else None

    # Snapshot both registries: they are weak sets that apply() and
    # remove() on other threads may be changing.

    candidates: list[Observer] = [
        *[bnd for bnd in list(_applied_bindings) if bnd.applied],
        *list(_observed_callables),
    ]

    found = [
        observer
        for observer in candidates
        if (path is None or observer.path == path)
        and (label is None or _display(observer) == label)
    ]

    return sorted(found, key=_order)


def find_binding(
    target: Any = None,
    *attrs: str,
    label: str | None = None,
    attr: str | None = None,
    item: Any = MISSING,
) -> Observer:
    """The one applied binding, or live observed() proxy, at a location
    and/or with a label; see find_bindings() for how each is matched.

    Raises NoBindingError when nothing matches and AmbiguousBindingError
    when more than one does, as on a target with stacked bindings, where
    a label tells them apart or find_bindings() returns all of them.
    """

    found = find_bindings(target, *attrs, label=label, attr=attr, item=item)

    # Describe the query in the caller's own terms for the errors.

    parts: list[str] = []
    if target is not None:
        parts.append(_query_path(target, attrs, attr, item))
    if label is not None:
        parts.append(f"label {label!r}")
    query = " with ".join(parts)

    if not found:
        raise NoBindingError(f"no applied binding matches {query}")

    if len(found) > 1:
        names = ", ".join(_display(observer) for observer in found)
        raise AmbiguousBindingError(
            f"{len(found)} bindings match {query}: {names}. Add a label to"
            f" the query, or use find_bindings() for all of them."
        )

    return found[0]


def bindings_of(obj: Any) -> list[Observer]:
    """Every binding whose wrapper is a layer of `obj`, outermost first.

    The inverse of Binding.is_wrapping(): rather than asking whether an
    object in hand carries one particular binding, this says which
    bindings it carries. `obj` is a wrapped callable read from a module
    or class, a method read through an instance, a from-import copy, a
    WSGI or ASGI application, or an `@observed` function, and its
    wrapper chain is walked seeing through proxies and later decorators.
    An attribute binding's descriptor is recognised when read off the
    class dict (`vars(Owner)["name"]`), not through the value it
    returns. A binding since removed is still recognised from its
    retired wrapper, with `removed` saying so.
    """

    # A method read through an instance or class is a bound wrapper
    # whose parent is the installed layer.

    if isinstance(obj, BoundFunctionWrapper):
        obj = obj._self_parent

    found: list[Observer] = []

    for layer in wrapper_chain(obj):
        if isinstance(layer, ObservedCallable):
            found.append(layer)
            continue

        binding = getattr(layer, "_self_wrapture_binding", None)
        if binding is not None:
            found.append(binding)

    return found


def binding_of(obj: Any) -> Observer | None:
    """The binding whose wrapper `obj` is, or the outermost of them when
    several are stacked; None when nothing in its wrapper chain is
    wrapture's. See bindings_of() for what is recognised.
    """

    found = bindings_of(obj)

    return found[0] if found else None
