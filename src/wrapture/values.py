"""Slot access for value bindings, and content access for mapping bindings.

A value binding holds a value in a slot of an owner object for as long
as it is applied: an attribute (`attr=`) or a mapping entry (`item=`).
These helpers read, write, delete and test the slot uniformly for the
two kinds, and capture the prior state that remove() puts back. A
mapping binding does the same for the whole content of one mapping
object, which is mutated in place so every holder of it sees the
change; its helpers snapshot, write and restore that content.
"""

from __future__ import annotations

import importlib
from collections.abc import MutableMapping
from typing import Any

import wrapt
from wrapt import MISSING


def resolve_owner(target: Any, name: str) -> Any:
    """The object holding the slot: the target itself when the location
    has no steps, else whatever the dotted name resolves to on it. A
    string target is imported."""

    if name:
        return wrapt.resolve_path(target, name)[2]

    if isinstance(target, str):
        return importlib.import_module(target)

    return target


def slot_prior(owner: Any, kind: str, key: Any) -> Any:
    """What the slot holds now, or MISSING when it holds nothing.

    For an attribute only what sits in the owner's own namespace counts,
    never a value inherited from a class or served by __getattr__, so
    restoring recreates exactly the prior state of that owner. An owner
    without a namespace dictionary (slots, extension types) falls back
    to plain attribute lookup.
    """

    if kind == "item":
        try:
            present = key in owner
        except TypeError:
            present = False

        return owner[key] if present else MISSING

    namespace = getattr(owner, "__dict__", None)
    if namespace is not None:
        return namespace.get(key, MISSING)

    return getattr(owner, key, MISSING)


def slot_present(owner: Any, kind: str, key: Any) -> bool:
    """Whether the slot currently holds anything, by the same rule as
    slot_prior()."""

    return slot_prior(owner, kind, key) is not MISSING


def slot_read(owner: Any, kind: str, key: Any) -> Any:
    """What the slot holds now, or MISSING; the same as slot_prior()."""

    return slot_prior(owner, kind, key)


def slot_write(owner: Any, kind: str, key: Any, value: Any) -> None:
    if kind == "item":
        owner[key] = value
    else:
        setattr(owner, key, value)


def slot_delete(owner: Any, kind: str, key: Any) -> None:
    """Make the slot absent; a no-op when it already is."""

    if not slot_present(owner, kind, key):
        return

    if kind == "item":
        del owner[key]
    else:
        delattr(owner, key)


def slot_restore(owner: Any, kind: str, key: Any, prior: Any) -> None:
    """Put the slot back to `prior`, deleting it when prior is MISSING."""

    if prior is MISSING:
        slot_delete(owner, kind, key)
    else:
        slot_write(owner, kind, key, prior)


# -- whole-mapping content ------------------------------------------------


def check_mapping(label: str, obj: Any) -> Any:
    """Refuse anything a mapping binding cannot substitute the content
    of: it needs the mutable mapping protocol (clear, update, items),
    so a read-only proxy or a non-mapping is a TypeError."""

    if not isinstance(obj, MutableMapping):
        raise TypeError(
            f"{label}: mode='mapping' needs a mutable mapping whose content"
            f" is substituted in place, got {type(obj).__name__}"
        )

    return obj


def mapping_snapshot(mapping: Any) -> list[tuple[Any, Any]]:
    """The content at apply(), as (key, value) pairs in order, so
    remove() can put back exactly what was there."""

    return list(mapping.items())


def mapping_restore(mapping: Any, snapshot: Any) -> None:
    """Make the content exactly the snapshot again, in its order."""

    if snapshot is MISSING:
        return

    mapping.clear()
    for key, value in snapshot:
        mapping[key] = value


def mapping_write(mapping: Any, state: str, values: Any, snapshot: Any) -> None:
    """Put the content into the configured state, relative to the
    snapshot taken at apply(): `overrides` replaces it, `updates`
    merges over the original content, and anything else restores it."""

    if state == "overrides":
        mapping.clear()
        mapping.update(values)
    elif state == "updates":
        mapping_restore(mapping, snapshot)
        mapping.update(values)
    else:
        mapping_restore(mapping, snapshot)


def mapping_matches(mapping: Any, state: str, values: Any, snapshot: Any) -> bool:
    """Whether the content is still what the binding put there."""

    if state == "overrides":
        return dict(mapping.items()) == dict(values)

    if state == "updates":
        return all(
            key in mapping and mapping[key] == value for key, value in values.items()
        )

    return dict(mapping.items()) == dict(snapshot)
