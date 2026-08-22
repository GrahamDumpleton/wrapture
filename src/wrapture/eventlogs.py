"""The filterable view over recorded events.

One naming rule, without exception, on every object in this package: a
method whose name starts with assert_ raises on failure; everything else
returns data. The filters here narrow and return a new log, never raise,
so a mismatched filter yields an empty log rather than an error. Each
narrowed log remembers what it was filtered from, so failure output can
show the events a filter discarded instead of a bare empty result.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from fnmatch import fnmatchcase
from typing import Any

from wrapt import MISSING

from .events import Event


def _levelno(level: int | str) -> int:
    """Resolve a logging level given as either a name or a number.

    Names resolve through the logging module's registered levels, so
    custom levels added with logging.addLevelName() work; numbers pass
    through as they are.
    """

    if isinstance(level, bool):
        raise ValueError(f"level must be a logging level name or number, got {level!r}")
    if isinstance(level, int):
        return level

    if isinstance(level, str):
        mapping = logging.getLevelNamesMapping()
        try:
            return mapping[level.upper()]
        except KeyError:
            known = ", ".join(sorted(mapping))
            raise ValueError(
                f"unknown logging level {level!r}; known names are {known}"
            ) from None

    raise ValueError(f"level must be a logging level name or number, got {level!r}")


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

    def in_phase(self, index: int) -> EventLog:
        """Events handled by phase `index` of a phased binding. Events of
        a binding with a single phase carry no phase and never match."""

        return self._narrow(f"[in_phase={index}]", lambda event: event.phase == index)

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
        matches a call that never spelled the default out. A name that
        is not a parameter falls through into the target's var-keyword
        bundle when the signature has one: for a target declaring
        **options, with_args(parent_id="root") matches a call that
        passed parent_id through **options, other bundle keys free.
        Naming the bundle parameter itself compares the whole mapping,
        as for any other parameter, so with_args(options={...}) is the
        exact form. Events with no normalized arguments, attribute
        events included, never match.
        """

        suffix = "[" + ", ".join(f"{k}={v!r}" for k, v in arguments.items()) + "]"

        def matches(event: Event, name: str, value: Any) -> bool:
            recorded = event.arguments
            assert recorded is not None

            # A parameter name compares against its recorded value,
            # the var-keyword bundle included.

            if name in recorded:
                return bool(recorded[name] == value)

            # Anything else falls through into the bundle when the
            # signature has one; a missing key is no match.

            if event.var_keyword is None:
                return False

            bundle = recorded.get(event.var_keyword)
            if not isinstance(bundle, Mapping) or name not in bundle:
                return False

            return bool(bundle[name] == value)

        def keep(event: Event) -> bool:
            if event.arguments is None:
                return False

            return all(matches(event, name, value) for name, value in arguments.items())

        return self._narrow(suffix, keep)

    def with_instance(self, instance: Any) -> EventLog:
        """Events recorded against exactly this object: calls made on
        it as the bound instance, with classmethod calls recording the
        class. Comparison is by identity, not equality, so two
        equal-but-distinct instances stay distinguishable; anything
        looser is a matching() predicate. Events with no bound
        instance, module-level functions included, never match.
        """

        suffix = f"[instance={instance!r}]"

        def keep(event: Event) -> bool:
            return event.instance is not None and event.instance is instance

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

    def at_level(self, level: int | str) -> EventLog:
        """Log events at or above the given severity.

        The level is a name ("WARNING") or a number (logging.WARNING),
        and the comparison is by number, so at_level("WARNING") means
        "at least this severe". Only log events carry a level, so
        anything else never matches.
        """

        threshold = _levelno(level)

        def keep(event: Event) -> bool:
            levelno = event.data.get("levelno")
            return isinstance(levelno, int) and levelno >= threshold

        return self._narrow(f"[at_level={level}]", keep)

    def with_message(self, pattern: str) -> EventLog:
        """Log events whose message matches the fnmatch pattern.

        Matching is case-sensitive fnmatch, the same convention config
        filters use, so "*declined*" finds a substring. Only log
        events carry a message, so anything else never matches.
        """

        def keep(event: Event) -> bool:
            message = event.data.get("message")
            return isinstance(message, str) and fnmatchcase(message, pattern)

        return self._narrow(f"[message={pattern!r}]", keep)

    def without_message(self, pattern: str) -> EventLog:
        """Log events whose message does not match the fnmatch pattern:
        the negation of with_message(), for asserting that noise aside,
        nothing else was logged. Only log events carry a message, so
        anything else never matches."""

        def keep(event: Event) -> bool:
            message = event.data.get("message")
            return isinstance(message, str) and not fnmatchcase(message, pattern)

        return self._narrow(f"[message!={pattern!r}]", keep)

    def injected(self, want: bool = True) -> EventLog:
        """Events whose outcome was supplied by returns(), raises() or
        rejects(); with want=False, events whose outcome was real."""

        suffix = "[injected]" if want else "[injected=False]"
        return self._narrow(suffix, lambda event: event.injected is want)

    def finished(self) -> EventLog:
        """Events whose operation has ended, however it ended: the call
        returned or raised, the generator closed, the coroutine was
        awaited. For an `async def` target this is the awaited subset,
        so `finished().assert_once()` says "awaited once"."""

        return self._narrow("[finished]", lambda event: event.finished)

    def pending(self) -> EventLog:
        """Events whose operation has not ended: a call still in flight,
        a generator still being consumed, a coroutine created and not
        yet awaited. After the code under test has finished, a pending
        event on an `async def` target is a call that was never
        awaited."""

        return self._narrow("[pending]", lambda event: not event.finished)

    # -- assertions: raise on failure, return self so they chain -------------

    def assert_never(self) -> EventLog:
        """Assert the log holds no events."""

        if self._events:
            raise AssertionError(self._failure("expected no events"))
        return self

    def assert_any(self) -> EventLog:
        """Assert the log holds at least one event."""

        if not self._events:
            raise AssertionError(self._failure("expected at least 1 event(s)"))
        return self

    def assert_once(self) -> EventLog:
        """Assert the log holds exactly one event."""

        return self.assert_times(1)

    def assert_times(self, count: int) -> EventLog:
        """Assert the log holds exactly `count` events."""

        if len(self._events) != count:
            raise AssertionError(self._failure(f"expected exactly {count} event(s)"))
        return self

    def assert_at_least(self, count: int) -> EventLog:
        """Assert the log holds at least `count` events."""

        if len(self._events) < count:
            raise AssertionError(self._failure(f"expected at least {count} event(s)"))
        return self

    def assert_at_most(self, count: int) -> EventLog:
        """Assert the log holds at most `count` events."""

        if len(self._events) > count:
            raise AssertionError(self._failure(f"expected at most {count} event(s)"))
        return self

    def _describe(self) -> list[str]:
        # The log's events, line by line. When the log is empty because
        # a filter discarded everything, fall back to the nearest
        # non-empty log in the filter chain, so filtering the wrong
        # thing is visible rather than mysterious.

        lines = repr(self).splitlines()

        if not self._events:
            ancestor = self._filtered_from
            while ancestor is not None and not ancestor._events:
                ancestor = ancestor._filtered_from

            if ancestor is not None:
                lines.append("  filtered from:")
                lines.extend(f"    {line}" for line in repr(ancestor).splitlines())

        return lines

    def _failure(self, expectation: str) -> str:
        return "\n".join([f"{expectation}, got {len(self._events)}", *self._describe()])

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
