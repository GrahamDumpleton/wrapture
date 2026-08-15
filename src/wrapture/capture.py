"""Capture policies: how much of a call's values recording stores.

Recording by reference is free but can lie retroactively: mutate a list
after the call and the tape shows the mutated list. Copying everything
is safe but costly, and calling repr() on arbitrary values can itself
have side effects. So capture is a policy, chosen per binding or
declared by the sink consuming the events.

A policy is either one of the levels below, ordered by cost, or a
callable fn(name, value) -> stored applied to each captured value:

    NONE        no arguments, no result; skips signature binding
    TYPES       type names only; never calls user code
    REFERENCE   store references (what unittest.mock does); the default
    SUMMARY     bounded repr; survives locks and sockets, retains nothing
    SNAPSHOT    deepcopy; highest fidelity, falls back where it raises

The safe levels are the ones that call no user code: NONE, TYPES and
REFERENCE cannot trigger anything, while SUMMARY and SNAPSHOT execute
methods on the values being captured, which may be slow, may raise, and
may have effects. That is why REFERENCE is the default.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

NONE = 0
TYPES = 1
REFERENCE = 2
SUMMARY = 3
SNAPSHOT = 4

CapturePolicy = int | Callable[[str | None, Any], Any]

_ATOMIC = (bool, int, float, complex, type(None))


def type_name(value: Any) -> str:
    """The safest possible capture: a type name, calling no user code.

    repr() can have side effects (a lazy ORM object may issue a query),
    so tracing with SUMMARY can cause the very behaviour being traced.
    TYPES is immune because it touches nothing but the class.
    """

    return f"<{type(value).__name__}>"


def summarize(value: Any, *, limit: int = 200, items: int = 10) -> Any:
    """Bounded repr. Never raises, never retains the original.

    Type-aware for the common containers, because a naive
    repr(value)[:limit] materialises the whole repr before truncating:
    a 5MB repr costs 5MB and milliseconds even though the output is 200
    bytes. Handling str, bytes and the container types structurally
    bounds the work as well as the result.

    Unavoidably, the fallback for unknown types calls repr(), which is
    user code: it may be slow and may have side effects. Use TYPES where
    that matters.
    """

    if isinstance(value, _ATOMIC):
        return value

    if isinstance(value, (str, bytes)):
        if len(value) > limit:
            if isinstance(value, bytes):
                return value[:limit] + b"..."
            return value[:limit] + f"...+{len(value) - limit}"
        return value

    if isinstance(value, (list, tuple, set, frozenset)):
        kind = type(value).__name__
        shown = [
            summarize(v, limit=limit // 4, items=items) for v in list(value)[:items]
        ]
        more = f", +{len(value) - items}" if len(value) > items else ""
        return f"<{kind} {shown!r}{more}>"

    if isinstance(value, dict):
        shown_items = {
            k: summarize(v, limit=limit // 4, items=items)
            for k, v in list(value.items())[:items]
        }
        more = f", +{len(value) - items}" if len(value) > items else ""
        return f"<dict {shown_items!r}{more}>"

    try:
        text = repr(value)
    except Exception as exc:
        return f"<unreprable {type(value).__name__}: {type(exc).__name__}>"

    if len(text) > limit:
        return text[:limit] + f"...+{len(text) - limit}"
    return text


def redact(
    *names: str, level: CapturePolicy = REFERENCE, marker: str = "<redacted>"
) -> CapturePolicy:
    """A capture policy that replaces named parameters with a marker.

        binding(Gateway, "charge", capture_args=redact("card_number"))

    Matching is by parameter name against the signature-normalized
    arguments, so it works whether the caller passed the value
    positionally or by keyword. Everything not named is captured at
    `level`.

    Two limits: results have no parameter name, so a bare redact() does
    not touch them (pair it with capture_result=NONE when the secret
    comes back out); and names are top-level parameters only, so a
    secret nested inside a dict argument is not found. Any custom
    fn(name, value) callable handles those cases.
    """

    wanted = set(names)

    def policy(name: str | None, value: Any) -> Any:
        if name in wanted:
            return marker
        return _capture_value(level, name, value)

    policy.level = _level_of(level)  # type: ignore[attr-defined]
    return policy


def _level_of(policy: CapturePolicy) -> int:
    # A policy is an int level or a callable carrying one; a callable
    # without a declared level is assumed to want normalized arguments.

    if callable(policy):
        return int(getattr(policy, "level", REFERENCE))
    return policy


def _capture_value(policy: CapturePolicy, name: str | None, value: Any) -> Any:
    if callable(policy):
        return policy(name, value)
    return _apply_capture(policy, value)


def _apply_capture(level: int, value: Any) -> Any:
    if level == TYPES:
        return type_name(value)

    if level >= SNAPSHOT:
        # deepcopy fails on locks, sockets, file handles and connections,
        # which are common arguments. Failing the call under test because
        # the recorder could not copy an argument would be indefensible,
        # so fall back to the bounded repr instead.

        try:
            return copy.deepcopy(value)
        except Exception:
            return summarize(value)

    if level == SUMMARY:
        return summarize(value)

    return value
