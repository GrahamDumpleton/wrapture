"""The filterable view over recorded events.

One naming rule, without exception, on every object in this package: a
method whose name starts with assert_ raises on failure; everything else
returns data. The filters here narrow and return a new log, never raise,
so a mismatched filter yields an empty log rather than an error. Each
narrowed log remembers what it was filtered from, so failure output can
show the events a filter discarded instead of a bare empty result.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from wrapt import MISSING

from .events import Event


class EventLog:
    """An immutable, filterable view over recorded events.

    Filters return a narrowed EventLog and never raise. The data
    accessors make a log usable directly in a bare assert: an empty log
    is falsey, and repr() prints the events it holds.
    """

    def __init__(
        self,
        label: str,
        events: list[Event],
        *,
        filtered_from: EventLog | None = None,
    ) -> None:
        self._label = label
        self._events = list(events)
        self._filtered_from = filtered_from

    @property
    def label(self) -> str:
        """The log's provenance: the binding label plus one bracketed
        segment per filter applied."""

        return self._label

    def _narrow(self, suffix: str, keep: Callable[[Event], bool]) -> EventLog:
        kept = [event for event in self._events if keep(event)]
        return EventLog(f"{self._label}{suffix}", kept, filtered_from=self)

    # -- filters: return a narrowed log, never raise -------------------------

    def of_kind(self, *kinds: str) -> EventLog:
        """Events of the given kind or kinds: "call", "get", "set" or
        "delete"."""

        wanted = set(kinds)
        return self._narrow(f"[{','.join(kinds)}]", lambda event: event.kind in wanted)

    def matching(self, predicate: Callable[[Event], bool]) -> EventLog:
        """Events for which the predicate returns true."""

        name = getattr(predicate, "__name__", "predicate")
        return self._narrow(f"[matching={name}]", lambda event: bool(predicate(event)))

    def raising(self, *exceptions: type[BaseException]) -> EventLog:
        """Events that raised one of the given exception types, or, with
        no arguments, events that raised anything at all."""

        if not exceptions:
            return self._narrow("[raising]", lambda event: event.exception is not None)

        names = ",".join(exception.__name__ for exception in exceptions)
        return self._narrow(
            f"[raising={names}]",
            lambda event: isinstance(event.exception, exceptions),
        )

    def with_args(self, **arguments: Any) -> EventLog:
        """Call events whose normalized arguments include every given
        name and value.

        Matching is by parameter name against the signature-normalized
        arguments with defaults applied, so with_args(currency="USD")
        matches a call that never spelled the default out. Events with
        no normalized arguments, attribute events included, never match.
        """

        suffix = "[" + ", ".join(f"{k}={v!r}" for k, v in arguments.items()) + "]"

        def keep(event: Event) -> bool:
            if event.arguments is None:
                return False

            return all(
                name in event.arguments and event.arguments[name] == value
                for name, value in arguments.items()
            )

        return self._narrow(suffix, keep)

    def returning(self, value: Any) -> EventLog:
        """Events whose recorded outcome equals the value: the return
        value of a call, or the value a read produced. Events that
        raised have no outcome and never match."""

        return self._narrow(
            f"[returning={value!r}]",
            lambda event: event.result is not MISSING and event.result == value,
        )

    def with_value(self, value: Any) -> EventLog:
        """Set events that wrote the value: the write-side counterpart
        to with_args()."""

        return self._narrow(
            f"[value={value!r}]",
            lambda event: event.value is not MISSING and event.value == value,
        )

    # -- data ----------------------------------------------------------------

    @property
    def count(self) -> int:
        """How many events the log holds."""

        return len(self._events)

    @property
    def first(self) -> Event:
        """The earliest event in the log."""

        return self._events[0]

    @property
    def last(self) -> Event:
        """The latest event in the log."""

        return self._events[-1]

    def __len__(self) -> int:
        return len(self._events)

    def __bool__(self) -> bool:
        return bool(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __getitem__(self, index: int) -> Event:
        return self._events[index]

    def __repr__(self) -> str:
        lines = [f"<EventLog {self._label}: {len(self._events)} event(s)>"]

        if self._events:
            lines.extend(f"    {event}" for event in self._events)
        else:
            lines.append("    (no events)")

        return "\n".join(lines)
