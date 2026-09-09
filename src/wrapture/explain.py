"""Describing configured behaviour in words.

A binding's repr says whether it intervenes at all; explain() says what
it is set up to do, as a multi-line string meant for a person at a
prompt or in a notebook rather than for a program. The describers here
read the notes the behaviour verbs leave beside the closures they build
(see Phase.notes), the request hooks, and the holding state of a value
or mapping binding, so nothing has to be reconstructed from a closure.

Values are summarised per line: the first level of a container is
spelled out, and a nested container whose rendering runs long collapses
to a typed placeholder such as `<dict 2 keys>`, so one large canned
result cannot swamp the description. Callables are named; a lambda shows
as `<lambda>` and anything without a name falls back to its repr. That
is a fair description of what the caller wrote, and the description
never raises.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from wrapt import MISSING

from .capture import _LEVEL_NAMES

if TYPE_CHECKING:
    from .behaviours import Phase
    from .bindings import Binding, BindingGroup
    from .iterators import IteratorProxy

# What one operation of each kind is called when a phase's exit
# condition counts them.

_OPERATION_NOUNS = {
    "call": "calls",
    "get": "reads",
    "set": "writes",
    "delete": "deletes",
}

_CHANNELS = {
    "callable": ("call",),
    "attribute": ("get", "set", "delete"),
}

_CHANNEL_NAMES = {
    "call": "on_call",
    "get": "on_get",
    "set": "on_set",
    "delete": "on_delete",
}

_ITERATOR_CHANNEL_NAMES = {
    "item": "on_item",
    "finish": "on_finish",
    "error": "on_error",
    "abandon": "on_abandon",
}

PASSES_THROUGH = "passes through"


# -- values and callables ------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + f"...+{len(text) - limit}"
    return text


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def describe_callable(fn: Any) -> str:
    """Name a callable the way its author would: a function or class by
    its name, a method by class and name, a partial by what it wraps."""

    if isinstance(fn, functools.partial):
        return f"partial({describe_callable(fn.func)})"

    # A function defined inside another carries the enclosing function
    # in its qualified name, which says where it was written rather
    # than what it is, so the plain name is used for those.

    qualname = getattr(fn, "__qualname__", None)
    name = getattr(fn, "__name__", None)

    if isinstance(qualname, str):
        return name if "<locals>" in qualname and isinstance(name, str) else qualname

    if isinstance(name, str):
        return name

    return describe_value(fn)


def describe_value(value: Any, *, limit: int = 100, nested: int = 32) -> str:
    """One line for a configured value. Never raises."""

    if value is None or isinstance(value, (bool, int, float, complex)):
        return repr(value)

    if isinstance(value, (str, bytes)):
        return _clip(repr(value), limit)

    if isinstance(value, dict):
        try:
            parts = [
                f"{k!r}: {describe_value(v, limit=nested, nested=nested)}"
                for k, v in value.items()
            ]
        except Exception:
            return f"<dict {_plural(len(value), 'key')}>"

        text = "{" + ", ".join(parts) + "}"
        return text if len(text) <= limit else f"<dict {_plural(len(value), 'key')}>"

    if isinstance(value, (list, tuple, set, frozenset)):
        kind = type(value).__name__
        open_, close = {
            "list": ("[", "]"),
            "tuple": ("(", ")"),
        }.get(kind, (f"{kind}({{", "})"))

        try:
            parts = [describe_value(v, limit=nested, nested=nested) for v in value]
        except Exception:
            return f"<{kind} {_plural(len(value), 'item')}>"

        if kind == "tuple" and len(parts) == 1:
            parts[0] += ","

        text = open_ + ", ".join(parts) + close
        return text if len(text) <= limit else f"<{kind} {_plural(len(value), 'item')}>"

    if isinstance(value, BaseException):
        try:
            return _clip(repr(value), limit)
        except Exception:
            return f"{type(value).__name__}(...)"

    if callable(value):
        return describe_callable(value)

    try:
        return _clip(repr(value), limit)
    except Exception as exc:
        return f"<unreprable {type(value).__name__}: {type(exc).__name__}>"


def _describe_argument(label: str, argument: Any) -> list[str]:
    """The lines for one (verb, argument) note: the verb and its
    argument on one line, with an iterator proxy handed to a stage
    described beneath it, since the proxy carries behaviour of its own."""

    from .iterators import IteratorProxy, _IteratorBehaviour

    if isinstance(argument, _IteratorBehaviour):
        argument = argument._factory

    if isinstance(argument, IteratorProxy):
        return [f"{label}: {argument!r}", *_indent(explain_iterator(argument, True))]

    if argument is None and label == "rejects":
        return [label]

    if label == "returns from":
        return [f"{label}: {_describe_sequence(argument)}"]

    if callable(argument) and not isinstance(argument, type):
        return [f"{label}: {describe_callable(argument)}"]

    return [f"{label}: {describe_value(argument)}"]


def _describe_sequence(iterable: Any) -> str:
    # A concrete sequence is listed; a generator or itertools object
    # cannot be shown without consuming it, so describe_value() falls
    # back to its repr.

    return describe_value(iterable)


# -- layout ------------------------------------------------------------------


def _indent(lines: Iterable[str], by: int = 2) -> list[str]:
    return [" " * by + line for line in lines]


def _labelled(label: str, lines: list[str], width: int) -> list[str]:
    """`lines` beside `label`, the label padded to `width` and the
    continuation lines aligned under the first."""

    if not lines:
        return [label]

    first = label.ljust(width) + lines[0]
    return [first, *(" " * width + line for line in lines[1:])]


def _blocks(blocks: list[tuple[str, list[str]]], gap: int = 2) -> list[str]:
    """Several labelled blocks with one shared label column."""

    width = max(len(label) for label, _ in blocks) + gap
    lines: list[str] = []

    for label, block in blocks:
        lines.extend(_labelled(label, block, width))

    return lines


# -- phases --------------------------------------------------------------------


def describe_phase(phase: Phase) -> list[str]:
    """What one phase does: its stages in order, then its terminal."""

    lines: list[str] = []

    for label, argument in phase.notes:
        lines.extend(_describe_argument(label, argument))

    if phase.terminal_note is not None:
        lines.extend(_describe_argument(*phase.terminal_note))

    return lines or [PASSES_THROUGH]


def describe_exit(phase: Phase, operation: str) -> str | None:
    """How the phase hands over, or None when nothing would end it."""

    noun = _OPERATION_NOUNS.get(operation, "operations")
    conditions: list[str] = []

    if phase.exit is not None:
        kind, argument = phase.exit
        if kind == "after":
            conditions.append(f"after {_plural(argument, noun[:-1])}")
        else:
            conditions.append(f"when {describe_callable(argument)}(event) is true")

    if phase.source is not None:
        if phase.successor is None:
            conditions.append(
                "when the sequence is exhausted, raising SequenceExhaustedError"
            )
        else:
            conditions.append("when the sequence is exhausted")

    if phase.successor is not None and not conditions:
        conditions.append("on advance()")

    if not conditions:
        return None

    return "ends " + ", or ".join(conditions)


def describe_chain(head: Phase | None, active: int, operation: str) -> list[str]:
    """Every phase of one operation, labelled when there is more than
    one, with where the chain has got to."""

    if head is None:
        return [PASSES_THROUGH]

    if head.successor is None:
        lines = describe_phase(head)
        ending = describe_exit(head, operation)
        return lines + ([ending] if ending else [])

    blocks: list[tuple[str, list[str]]] = []
    phase: Phase | None = head

    while phase is not None:
        lines = describe_phase(phase)
        ending = describe_exit(phase, operation)
        if ending:
            lines.append(ending)
        blocks.append((f"phase {phase.index}", lines))
        phase = phase.successor

    return _blocks(blocks) + [f"now in phase {active}"]


# -- bindings ----------------------------------------------------------------


def _describe_policy(policy: Any) -> str:
    if isinstance(policy, int):
        for name, level in _LEVEL_NAMES.items():
            if level == policy:
                return name
        return str(policy)

    description = getattr(policy, "description", None)
    if isinstance(description, str):
        return description

    return describe_callable(policy)


def describe_options(binding: Binding) -> str | None:
    """The options that shape what the binding records, or None when it
    records with the defaults."""

    from .filters import RequestFilter

    parts: list[str] = []

    # A request filter given as when= was adapted to a predicate at
    # construction; the filter itself is kept for this description.

    when = binding._request_filter or binding._when
    if isinstance(when, RequestFilter):
        records = f"records: {when!r}"
    elif when is None:
        records = ""
    elif isinstance(when, bool):
        records = "" if when else "records nothing"
    elif callable(when):
        records = f"records when: {describe_callable(when)}"
    else:
        records = f"records when: {when!r}"

    if binding._tree:
        records = (records or "records") + ", declining the whole tree"

    if records:
        parts.append(records)

    args, result = binding._capture_args, binding._capture_result
    if args is not None and args is result:
        parts.append(f"capture: {_describe_policy(args)}")
    else:
        if args is not None:
            parts.append(f"capture args: {_describe_policy(args)}")
        if result is not None:
            parts.append(f"capture result: {_describe_policy(result)}")

    if binding._stack_depth is not None:
        parts.append(f"stack: {binding._stack_depth}")

    if binding._leaf:
        parts.append("leaf")

    category = binding._category
    if isinstance(category, str):
        parts.append(f"category: {category}")
    elif category is not None:
        parts.append(f"category: {describe_callable(category)}")

    return "; ".join(parts) or None


def _describe_holding(binding: Binding) -> list[str]:
    state, value = binding._holding
    lines: list[str] = []

    if binding._mode == "mapping":
        if state == "overrides":
            lines.append(f"replaces content: {describe_value(value)}")
        elif state == "updates":
            lines.append(f"updates in place: {describe_value(value)}")
        else:
            lines.append(PASSES_THROUGH)
    elif state == "overrides":
        lines.append(f"holds: {describe_value(value)}")
    elif state == "hides":
        lines.append("holds: absent")
    else:
        lines.append(PASSES_THROUGH)

    # Once applied the binding knows what it displaced, which is what
    # remove() will put back.

    if binding._value_applied:
        prior = binding._prior
        if binding._mode == "mapping":
            lines.append(f"puts back: {describe_value(dict(prior))}")
        elif prior is MISSING:
            lines.append("puts back: absent")
        else:
            lines.append(f"puts back: {describe_value(prior)}")

    return lines


def describe_request(binding: Binding) -> list[str]:
    """The request channel of a WSGI or ASGI binding."""

    hooks = binding._request_hooks
    inbound = "transforms environ" if binding._mode == "wsgi" else "transforms scope"
    lines: list[str] = []

    for fn in hooks["inbound"]:
        lines.append(f"{inbound}: {describe_callable(fn)}")
    for fn in hooks["response"]:
        lines.append(f"transforms response: {describe_callable(fn)}")
    for fn in hooks["body"]:
        lines.extend(_describe_argument("transforms body", fn))

    terminal = hooks["terminal"]
    if terminal is not None:
        kind, argument = terminal
        if kind == "returns":
            status, headers, body = argument
            lines.append(
                f"returns: {status}, {_plural(len(headers), 'header')},"
                f" body {describe_value(body)}"
            )
        else:
            lines.extend(_describe_argument(kind, argument))

    return lines or [PASSES_THROUGH]


def _channel_blocks(binding: Binding) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []

    for operation in _CHANNELS.get(binding._mode, ()):
        head = binding._heads.get(operation)
        if head is None:
            continue

        # A channel earns a place when any phase of it is configured, or
        # when then() gave it more than one phase, since a chain of
        # empty phases still changes what advance() and phase report.

        phase: Phase | None = head
        while phase is not None and not phase.configured:
            phase = phase.successor
        if phase is None and head.successor is None:
            continue

        lines = describe_chain(head, binding._phase_index(operation), operation)
        blocks.append((_CHANNEL_NAMES[operation], lines))

    return blocks


def explain_binding(binding: Binding) -> list[str]:
    """The lines of Binding.explain(): the repr, the recording options
    when any are set, then what each configured channel does."""

    lines = [repr(binding)]

    options = describe_options(binding)
    if options:
        lines.append(options)

    if binding._mode in ("value", "mapping"):
        return lines + _describe_holding(binding)

    if binding._mode in ("wsgi", "asgi"):
        request = describe_request(binding)
        if request == [PASSES_THROUGH]:
            return lines + request
        return lines + ["on_request", *_indent(request)]

    blocks = _channel_blocks(binding)
    if not blocks:
        return lines + [PASSES_THROUGH]

    # A channel with one phase sits beside its name; a phased channel
    # puts its name on a line of its own with the phases beneath it.

    if not any(_is_phased(block) for _, block in blocks):
        return lines + _blocks(blocks)

    width = max(len(name) for name, _ in blocks) + 2
    for name, block in blocks:
        if _is_phased(block):
            lines.append(name)
            lines.extend(_indent(block))
        else:
            lines.extend(_labelled(name, block, width))

    return lines


def _is_phased(block: list[str]) -> bool:
    return bool(block) and block[-1].startswith("now in phase")


def explain_channel(binding: Binding, operation: str) -> list[str]:
    """The lines of a base namespace's explain(): the whole chain for
    its operation."""

    return describe_chain(
        binding._heads.get(operation), binding._phase_index(operation), operation
    )


def explain_phase(phase: Phase, operation: str) -> list[str]:
    """The lines of a phase namespace's explain(): its own phase."""

    lines = describe_phase(phase)
    ending = describe_exit(phase, operation)
    return lines + ([ending] if ending else [])


def explain_group(group: BindingGroup) -> list[str]:
    """The lines of BindingGroup.explain(): each member under its name."""

    blocks = [
        (name, explain_binding(member)) for name, member in group._bindings.items()
    ]

    if not blocks:
        return [repr(group)]

    return [repr(group), *_blocks(blocks)]


# -- iterator proxies ----------------------------------------------------------


def explain_iterator(factory: IteratorProxy, channels_only: bool = False) -> list[str]:
    """The lines of IteratorProxy.explain(): each channel with behaviour
    under its name, or `passes through`."""

    blocks = [
        (_ITERATOR_CHANNEL_NAMES[channel], describe_iterator_channel(factory, channel))
        for channel in factory._CHANNELS
        if factory._notes[channel]
    ]

    body = _blocks(blocks) if blocks else [PASSES_THROUGH]
    return body if channels_only else [repr(factory), *body]


def describe_iterator_channel(factory: IteratorProxy, channel: str) -> list[str]:
    """One channel of an iterator proxy."""

    lines: list[str] = []
    for label, argument in factory._notes[channel]:
        lines.extend(_describe_argument(label, argument))

    return lines or [PASSES_THROUGH]
