"""Capturing log messages as recorded events.

A log message the observed application emits is an observation like
any other: it happened at a moment, inside some call, and it says
something a test may want to assert on or a trace may want to show in
place. This module records standard library logging as events of kind
"log", flowing through the same machinery as every other kind: onto a
timeline's tape for unit tests, and to process sinks for tracing,
with no consumer-specific capture anywhere.

The capture point is `logging.Logger.handle`, patched once, process
wide, the first time a capture is applied. `handle` is called exactly
once per emitted record, on the originating logger, before
propagation walks the handler hierarchy: one event per record, no
duplication, unaffected by `propagate` flags or whether the
application configured handlers at all, and nothing added to the
handler configuration the application owns. Level checks happen
upstream in the logging module's public methods, so capture respects
each logger's own thresholds: the boundary is "the application
emitted this record".

What is recorded is the record, not its formatted output: the message
is `record.getMessage()` (msg % args, never a traceback; the familiar
message-plus-traceback blob is manufactured later by a Formatter),
and an `exc_info` exception lands on the event's `exception` field,
where every consumer that understands failed calls already
understands it. Note the retention consequence: the live exception
keeps its traceback frames for the event's lifetime, the same
semantics call events have, which is a different profile under a
long-lived process sink than under a test's tape.

Capture selection is deliberately minimal: logger-name patterns,
a level threshold, and message exclusion patterns as a safety valve
for content that must never be captured anywhere. Selecting by
content is analysis, and analysis belongs to the consumers: query
filters on the tape (`at_level`, `with_message`), or a Filter
combinator in front of one sink.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from fnmatch import fnmatchcase
from typing import Any

from wrapt import wrap_function_wrapper

from .eventlogs import EventLog, _levelno
from .events import Event
from .exceptions import NeverAppliedError
from .sinks import _active_sinks, _in_recorder, _notify_exit, _record_event
from .timeline import _current_tape, _stack


def _patterns(value: str | Sequence[str], *, key: str) -> tuple[str, ...]:
    # Normalise a pattern argument: one string or a sequence of them,
    # each a non-empty fnmatch pattern.

    if isinstance(value, str):
        value = (value,)

    patterns = tuple(value)
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(
                f"{key} must be a non-empty fnmatch pattern or a list of"
                f" them, got {pattern!r}"
            )

    return patterns


class LogCapture:
    """A recorder of log messages, applied like a binding.

    Construct through capture_logs(). Applying registers the capture's
    selection; each record a logger emits that the selection accepts
    is recorded as one event of kind "log", delivered to whatever is
    listening exactly as a binding's events are: a timeline's tape, a
    process sink, or both. `events` reads the enclosing timeline's
    tape, so tests assert on logs with the same vocabulary they use
    for calls.

    Each applied capture records its own events for the records it
    selects, so two overlapping captures record a message twice, each
    event attributed to its capture. Keep selections disjoint where
    that matters.
    """

    def __init__(
        self,
        name: str | Sequence[str] = "*",
        *,
        level: int | str = "WARNING",
        exclude: str | Sequence[str] = (),
        exclude_message: str | Sequence[str] = (),
    ) -> None:
        self._names = _patterns(name, key="name")
        self._exclude = _patterns(exclude, key="exclude")
        self._exclude_message = _patterns(exclude_message, key="exclude_message")

        self._levelno = _levelno(level)
        self._level = logging.getLevelName(self._levelno)

        self._label = f"log[{','.join(self._names)}]"
        self._applied = False
        self._suspended = False
        self._apply_count = 0
        self._lock = threading.Lock()

        # How many records this capture has recorded, an honest
        # counter in the spirit of the sinks' own.

        self.captured = 0

    def __repr__(self) -> str:
        state = "applied" if self._applied else "not applied"
        return f"<LogCapture {','.join(self._names)} >={self._level} ({state})>"

    @property
    def label(self) -> str:
        """The display name events recorded by this capture carry."""

        return self._label

    @property
    def level(self) -> str:
        """The capture's level threshold, as a name."""

        return self._level

    @property
    def names(self) -> tuple[str, ...]:
        """The logger-name patterns this capture selects."""

        return self._names

    def apply(self) -> LogCapture:
        """Start capturing: register the selection, installing the
        process-wide logging patch on first use. Idempotent."""

        with self._lock:
            if not self._applied:
                self._applied = True
                self._apply_count += 1
                _register(self)

        return self

    def remove(self) -> LogCapture:
        """Stop capturing. Idempotent; the capture can be applied
        again afterwards, starting unsuspended."""

        with self._lock:
            if self._applied:
                self._applied = False
                self._suspended = False
                _deregister(self)

        return self

    def __enter__(self) -> LogCapture:
        return self.apply()

    def __exit__(self, *exc_info: Any) -> None:
        self.remove()

    def suspend(self) -> None:
        """Stop recording without removing: records pass through
        unrecorded until resume()."""

        self._suspended = True

    def resume(self) -> None:
        """Resume recording, undoing suspend()."""

        self._suspended = False

    @property
    def events(self) -> EventLog:
        """This capture's log events from the enclosing timeline, as a
        filterable EventLog.

        Raises rather than returning an empty log when no events could
        possibly exist, so "recorded nothing" can never be mistaken
        for "not recording": NeverAppliedError if the capture was
        never applied, and RuntimeError outside a timeline.
        """

        if self._apply_count == 0:
            raise NeverAppliedError(
                f"{self._label} was never applied; call apply() or use it"
                f" as a context manager"
            )

        tape = _current_tape()
        if tape is None:
            raise RuntimeError(
                f"{self._label}: events are only recorded inside a timeline()"
            )

        return tape.for_binding(self)

    def _selects(self, record: logging.LogRecord) -> bool:
        # Name and level selection, cheap enough to run per applied
        # capture on every record; the message exclusion runs later,
        # only once a message has been formatted.

        if self._suspended:
            return False

        if record.levelno < self._levelno:
            return False

        name = record.name
        if not any(fnmatchcase(name, pattern) for pattern in self._names):
            return False

        return not any(fnmatchcase(name, pattern) for pattern in self._exclude)

    def _excludes_message(self, message: str) -> bool:
        return any(fnmatchcase(message, pattern) for pattern in self._exclude_message)


def capture_logs(
    name: str | Sequence[str] = "*",
    *,
    level: int | str = "WARNING",
    exclude: str | Sequence[str] = (),
    exclude_message: str | Sequence[str] = (),
) -> LogCapture:
    """A capture of log messages, to apply like a binding.

    `name` is an fnmatch pattern (or list) over logger names, and
    `exclude` subtracts patterns from it. `level` is the threshold, a
    name or a number, meaning "at least this severe"; the default
    WARNING keeps capture volume deliberate rather than ambient.
    `exclude_message` names message patterns that must never be
    captured at all, the safety valve for sensitive content; every
    other selection by content belongs at query time or on a sink.

    Returns the capture unapplied: hand it to timeline() alongside
    bindings, use it as a context manager, or call apply() for the
    life of the process. Whichever way, `events` then reads the
    enclosing timeline's tape:

        logs = wrapture.capture_logs("myapp.*")

        with wrapture.timeline(charge, logs) as tape:
            place_order(declined_card)

        logs.events.at_level("WARNING").with_message("*declined*").assert_once()
    """

    return LogCapture(
        name, level=level, exclude=exclude, exclude_message=exclude_message
    )


# The applied captures, process wide. A tuple swapped under the lock,
# so the hot path reads it without locking.

_registry_lock = threading.Lock()
_captures: tuple[LogCapture, ...] = ()
_patched = False


def _register(capture: LogCapture) -> None:
    global _captures

    _install_patch()

    with _registry_lock:
        if capture not in _captures:
            _captures = _captures + (capture,)


def _deregister(capture: LogCapture) -> None:
    global _captures

    with _registry_lock:
        _captures = tuple(c for c in _captures if c is not capture)


def _install_patch() -> None:
    # Patch Logger.handle once, forever: like a post-import hook, the
    # wrapper cannot be uninstalled, so removal is deregistration and
    # the wrapper passes straight through when no captures are
    # applied.

    global _patched

    with _registry_lock:
        if _patched:
            return
        _patched = True

    wrap_function_wrapper(logging.Logger, "handle", _handle)


def _handle(
    wrapped: Any,
    instance: logging.Logger,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    # The recorder guard keeps a sink that logs (or anything else the
    # recording machinery calls) from recording recursively; the
    # record is captured before the handlers run, because the emission
    # is the fact being observed, whatever the handlers then do.

    if _captures and not _in_recorder.get():
        record = args[0] if args else kwargs.get("record")
        if isinstance(record, logging.LogRecord):
            _record_log(record)

    return wrapped(*args, **kwargs)


def _record_log(record: logging.LogRecord) -> None:
    # The same gate every binding uses: nobody listening, no event.

    active = _active_sinks()
    if not active:
        return

    selected = [capture for capture in _captures if capture._selects(record)]
    if not selected:
        return

    # Format the message once, under the recorder guard: msg % args
    # runs user __str__ methods, which must not record. A message that
    # cannot be formatted still records, saying so.

    guard = _in_recorder.set(True)
    try:
        try:
            message = record.getMessage()
        except Exception:
            message = f"<unformattable {record.msg!r}>"
    finally:
        _in_recorder.reset(guard)

    exception: BaseException | None = None
    if record.exc_info is not None and not isinstance(record.exc_info, bool):
        candidate = record.exc_info[1] if len(record.exc_info) > 1 else None
        if isinstance(candidate, BaseException):
            exception = candidate

    # Position under whatever is in progress, without pushing: the
    # event is instantaneous, so nothing can nest inside it.

    stack = _stack.get()
    parent = stack[-1] if stack else None

    for capture in selected:
        if capture._excludes_message(message):
            continue

        event = Event("log", record.name, binding=capture)
        event.data.update(
            level=record.levelname,
            levelno=record.levelno,
            message=message,
            module=record.module,
            funcName=record.funcName,
            lineno=record.lineno,
        )
        event.exception = exception

        event.parent_id = parent.seq if parent is not None else None
        event.depth = len(stack)
        event.started = time.perf_counter()

        # Enter, then close immediately: a log is instantaneous, and
        # the close is what makes it stream from sinks that write on
        # completion.

        _record_event(event, active)
        event.duration = 0.0
        _notify_exit(event, active)

        capture.captured += 1


__all__ = ["LogCapture", "capture_logs"]
