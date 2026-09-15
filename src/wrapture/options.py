"""The recording options an [[observe]] entry and an instrumentation
part share.

These are the keys wrapture owns that say how a group of bindings
records: capture, capture_args, capture_result, redact, redact_result,
redact_marker, leaf and stack, with enabled joining them under a part.
An observe entry carries them as its own keys and a part of an
instrumentation carries them as a sub-table, and they mean the same
thing in both places because both places check them here and compose
them here into the keyword arguments binding() and observed() take.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .capture import REFERENCE, CapturePolicy, _resolve_policy
from .capture import redact as _redact
from .exceptions import ConfigError
from .stacks import _resolve_depth

# The recording keys, in the order the documentation lists them; a
# part adds enabled ahead of them.

RECORDING_KEYS: tuple[str, ...] = (
    "capture",
    "capture_args",
    "capture_result",
    "redact",
    "redact_result",
    "redact_marker",
    "leaf",
    "stack",
)

PART_KEYS: tuple[str, ...] = ("enabled", *RECORDING_KEYS)


def _names(value: Any) -> tuple[str, ...]:
    # The string-or-list-of-strings shape a redact list takes; anything
    # else raises so the caller can name the key in its error.

    if isinstance(value, str):
        return (value,)

    if isinstance(value, Sequence) and all(isinstance(item, str) for item in value):
        return tuple(value)

    raise ValueError


def check_recording(options: Mapping[str, Any], where: str) -> None:
    """Check the recording keys present in `options`, raising
    ConfigError prefixed with `where` for a value binding() would
    refuse or a combination that could never act.

    A key absent or None is unset. Nothing outside the recording keys
    is looked at, so a caller with keys of its own checks those
    itself.
    """

    # The capture keys take the forms binding() accepts; a bad level
    # fails the load rather than the apply.

    for key in ("capture", "capture_args", "capture_result"):
        value = options.get(key)
        if value is None:
            continue

        try:
            _resolve_policy(value)
        except ValueError as exc:
            raise ConfigError(f"{where}: {key}: {exc}") from None

    redact = options.get("redact")
    if redact:
        try:
            _names(redact)
        except ValueError:
            raise ConfigError(
                f"{where}: redact must be a string or a list of strings, got {redact!r}"
            ) from None

    # Result redaction is a policy of its own on the result axis, so a
    # level for that axis alongside it could never act; the marker only
    # means something with a redaction to apply it to.

    redact_result = options.get("redact_result", False)
    if not isinstance(redact_result, bool):
        raise ConfigError(
            f"{where}: redact_result must be true or false, got {redact_result!r}"
        )

    if redact_result and options.get("capture_result") is not None:
        raise ConfigError(
            f"{where}: redact_result replaces the result with the marker, so"
            f" capture_result has nothing to apply to; use one or the other"
        )

    marker = options.get("redact_marker")
    if marker is not None:
        if not isinstance(marker, str) or not marker:
            raise ConfigError(
                f"{where}: redact_marker must be a non-empty string, got {marker!r}"
            )

        if not redact and not redact_result:
            raise ConfigError(
                f"{where}: redact_marker needs a redact list or"
                f" redact_result = true to apply to"
            )

    leaf = options.get("leaf")
    if leaf is not None and not isinstance(leaf, bool):
        raise ConfigError(f"{where}: leaf must be true or false, got {leaf!r}")

    # The stack depth takes the forms binding() accepts, checked here
    # so a bad value fails the load rather than the apply.

    stack = options.get("stack")
    if stack is not None:
        try:
            depth = _resolve_depth(stack)
        except ValueError as exc:
            raise ConfigError(f"{where}: {exc}") from None

        if isinstance(stack, bool) or depth is None or depth < 1:
            raise ConfigError(
                f"{where}: stack must be 'caller', 'full' or a positive frame"
                f" count, got {stack!r}"
            )


def compose_recording(
    options: Mapping[str, Any], capture: CapturePolicy | str | None = None
) -> dict[str, Any]:
    """The keyword arguments for binding() or observed() that the
    recording keys in `options` amount to, already checked.

    `capture` is the fallback level for an axis nothing in the options
    sets, the config's top-level level for an observe entry. Each axis
    key beats the options' own `capture`, which beats the fallback. A
    redact list then turns the arguments axis into a policy over its
    level: the named parameters become the marker and everything else
    captures at the level that axis resolved to. `redact_result`
    becomes the masking policy on the result axis.

    Only what is set is returned, so the result splats over a caller's
    own defaults without disturbing the axes the options leave alone.
    """

    level = options.get("capture")
    if level is None:
        level = capture

    args_level = options.get("capture_args")
    if args_level is None:
        args_level = level

    result_level = options.get("capture_result")
    if result_level is None:
        result_level = level

    marker = options.get("redact_marker")
    keywords = {} if marker is None else {"marker": marker}

    args_policy: CapturePolicy | str | None = args_level
    redact = options.get("redact")
    if redact:
        base = args_level if args_level is not None else REFERENCE
        args_policy = _redact(*_names(redact), level=base, **keywords)

    result_policy: CapturePolicy | str | None = result_level
    if options.get("redact_result"):
        result_policy = _redact(**keywords)

    composed: dict[str, Any] = {}

    if args_policy is not None:
        composed["capture_args"] = args_policy
    if result_policy is not None:
        composed["capture_result"] = result_policy

    stack = options.get("stack")
    if stack is not None:
        composed["stack"] = stack

    leaf = options.get("leaf")
    if leaf is not None:
        composed["leaf"] = leaf

    return composed
