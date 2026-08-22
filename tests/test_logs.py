"""Tests for capturing log messages as recorded events.

These cover the "log" event kind end to end: what a captured record
becomes, how capture selection works (names, levels, exclusions), how
log events nest inside the call tree, delivery to process sinks with
no timeline anywhere, the reentrancy guard, rendering by the printer
and the tape's tree, and the query-side sugar on EventLog.
"""

import io
import logging
from pathlib import Path

import pytest
from wrapt import MISSING

import wrapture
from wrapture import binding, capture_logs, observed, timeline
from wrapture.events import Event
from wrapture.sinks import Printer, Sink, _event_record


def logger_named(name: str) -> logging.Logger:
    # A fresh logger per test, enabled down to DEBUG and kept quiet:
    # no handlers and no propagation, so tests neither print nor
    # depend on the ambient logging configuration.

    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.propagate = False

    return log


class Collector(Sink):
    """A process sink recording enters and exits, for the tests that
    must not use a timeline."""

    capture_args = "none"
    capture_result = "none"

    def __init__(self) -> None:
        self.entered: list[Event] = []
        self.exited: list[Event] = []

    def on_enter(self, event: Event) -> None:
        self.entered.append(event)

    def on_exit(self, event: Event) -> None:
        self.exited.append(event)


# ---------------------------------------------------------------------------
# what one captured record becomes
# ---------------------------------------------------------------------------


def test_a_log_record_becomes_one_event_with_its_details() -> None:
    log = logger_named("orders.checkout")
    logs = capture_logs("orders.*")

    with timeline(logs):
        log.warning("card %s declined", "x123")

        event = logs.events.assert_once().first

        assert event.kind == "log"
        assert event.path == "orders.checkout"
        assert event.data["level"] == "WARNING"
        assert event.data["levelno"] == logging.WARNING
        assert event.data["message"] == "card x123 declined"
        assert event.data["lineno"] > 0
        assert event.data["funcName"]

        # Instantaneous: closed at capture, with no elapsed time.

        assert event.finished
        assert event.duration == 0.0
        assert event.result is MISSING


def test_the_event_renders_as_one_line() -> None:
    log = logger_named("orders.render")
    logs = capture_logs("orders.*")

    with timeline(logs):
        log.warning("line one\nline two")

        event = logs.events.first

    # The repr-escaped message keeps embedded newlines from ever
    # breaking a tree's alignment.

    assert str(event) == "log orders.render WARNING 'line one\\nline two'"
    assert "\n" not in str(event)


def test_a_logged_exception_lands_on_the_exception_field() -> None:
    log = logger_named("orders.exc")
    logs = capture_logs("orders.*", level="ERROR")

    with timeline(logs):
        try:
            raise ConnectionError("gateway down")
        except ConnectionError:
            log.exception("charge failed")

        event = logs.events.raising(ConnectionError).assert_once().first

        # The message stays clean: the traceback is the Formatter's
        # artifact, never part of the record's message.

        assert event.data["message"] == "charge failed"
        assert isinstance(event.exception, ConnectionError)


def test_an_unformattable_message_still_records() -> None:
    class Bad:
        def __str__(self) -> str:
            raise RuntimeError("no repr for you")

    log = logger_named("orders.bad")
    logs = capture_logs("orders.*")

    with timeline(logs):
        log.warning("value %s", Bad())

        event = logs.events.assert_once().first
        assert event.data["message"].startswith("<unformattable")


# ---------------------------------------------------------------------------
# capture selection: levels, names, exclusions
# ---------------------------------------------------------------------------


def test_records_below_the_threshold_are_not_captured() -> None:
    log = logger_named("orders.levels")
    logs = capture_logs("orders.*")

    with timeline(logs):
        log.info("routine")
        log.warning("trouble")

        assert [e.data["message"] for e in logs.events] == ["trouble"]


def test_the_threshold_accepts_a_name_or_a_number() -> None:
    log = logger_named("orders.forms")

    for level in ("DEBUG", logging.DEBUG):
        logs = capture_logs("orders.*", level=level)

        with timeline(logs):
            log.debug("fine detail")
            logs.events.assert_once()


def test_an_unknown_level_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown logging level"):
        capture_logs("orders.*", level="LOUD")


def test_name_patterns_select_and_exclude_loggers() -> None:
    orders = logger_named("orders.checkout")
    noise = logger_named("orders.health")
    other = logger_named("billing.invoices")

    logs = capture_logs("orders.*", exclude="orders.health*")

    with timeline(logs):
        orders.warning("kept")
        noise.warning("excluded by name")
        other.warning("outside the pattern")

        assert [e.data["message"] for e in logs.events] == ["kept"]


def test_exclude_message_blocks_capture_entirely() -> None:
    log = logger_named("orders.secrets")
    logs = capture_logs("orders.*", exclude_message="*card=*")

    with timeline(logs):
        log.warning("charge failed card=4111111111111111")
        log.warning("charge failed")

        assert [e.data["message"] for e in logs.events] == ["charge failed"]


def test_two_overlapping_captures_each_record_their_own_event() -> None:
    log = logger_named("orders.overlap")
    wide = capture_logs("orders.*")
    narrow = capture_logs("orders.overlap")

    with timeline(wide, narrow) as tape:
        log.warning("seen twice")

        wide.events.assert_once()
        narrow.events.assert_once()
        assert len([e for e in tape.all if e.kind == "log"]) == 2


def test_capture_arguments_are_validated() -> None:
    with pytest.raises(ValueError, match="name"):
        capture_logs("")
    with pytest.raises(ValueError, match="exclude"):
        capture_logs("orders.*", exclude=[""])


# ---------------------------------------------------------------------------
# position in the tree
# ---------------------------------------------------------------------------


def test_a_log_message_nests_inside_the_call_that_emitted_it() -> None:
    log = logger_named("orders.nested")

    @observed
    def work() -> None:
        log.warning("inside")

    logs = capture_logs("orders.*")

    with timeline(logs) as tape:
        work()
        log.warning("outside")

        inside, outside = list(logs.events)

        parent = tape.parent_of(inside)
        assert parent is not None and parent.kind == "call"
        assert inside.depth == 1

        assert tape.parent_of(outside) is None
        assert outside.depth == 0


def test_ordering_assertions_mix_calls_and_logs() -> None:
    log = logger_named("orders.order")

    @observed
    def attempt() -> None:
        log.warning("retrying")

    logs = capture_logs("orders.*")

    with timeline(logs) as tape:
        attempt()

        tape.assert_order(
            logs.events.with_message("retrying"),
        )


def test_tree_shows_the_message_in_place() -> None:
    log = logger_named("orders.tree")

    @observed
    def work() -> None:
        try:
            raise ValueError("bad")
        except ValueError:
            log.exception("failed")

    logs = capture_logs("orders.*", level="ERROR")

    with timeline(logs) as tape:
        work()

    lines = tape.tree().splitlines()
    assert lines[1] == "  log orders.tree ERROR 'failed'  !! ValueError"


# ---------------------------------------------------------------------------
# delivery: process sinks, no timeline anywhere
# ---------------------------------------------------------------------------


def test_logs_reach_a_process_sink_with_no_timeline() -> None:
    log = logger_named("orders.process")
    logs = capture_logs("orders.*")
    collector = Collector()

    wrapture.add_sink(collector)
    logs.apply()
    try:
        log.warning("no timeline anywhere")
    finally:
        logs.remove()
        wrapture.remove_sink(collector)

    assert [e.data["message"] for e in collector.exited] == ["no timeline anywhere"]
    assert collector.entered == collector.exited
    assert logs.captured == 1


def test_nothing_records_when_nothing_listens() -> None:
    log = logger_named("orders.idle")
    logs = capture_logs("orders.*")

    logs.apply()
    try:
        log.warning("nobody listening")
    finally:
        logs.remove()

    assert logs.captured == 0


def test_a_sink_that_logs_does_not_recurse() -> None:
    log = logger_named("orders.reentrant")

    class LoggingSink(Collector):
        def on_enter(self, event: Event) -> None:
            super().on_enter(event)
            log.warning("sink speaking")

    logs = capture_logs("orders.*")
    sink = LoggingSink()

    wrapture.add_sink(sink)
    logs.apply()
    try:
        log.warning("application speaking")
    finally:
        logs.remove()
        wrapture.remove_sink(sink)

    assert [e.data["message"] for e in sink.entered] == ["application speaking"]


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_events_raises_before_apply_and_outside_a_timeline() -> None:
    logs = capture_logs("orders.*")

    with pytest.raises(wrapture.NeverAppliedError):
        _ = logs.events

    with timeline(logs):
        pass

    with pytest.raises(RuntimeError, match="timeline"):
        _ = logs.events


def test_suspend_and_resume() -> None:
    log = logger_named("orders.suspend")
    logs = capture_logs("orders.*")

    with timeline(logs):
        logs.suspend()
        log.warning("while suspended")

        logs.resume()
        log.warning("after resume")

        assert [e.data["message"] for e in logs.events] == ["after resume"]


def test_the_capture_is_a_context_manager() -> None:
    log = logger_named("orders.ctx")
    logs = capture_logs("orders.*")
    collector = Collector()

    wrapture.add_sink(collector)
    try:
        with logs:
            log.warning("inside")
        log.warning("outside")
    finally:
        wrapture.remove_sink(collector)

    assert [e.data["message"] for e in collector.exited] == ["inside"]


def test_apply_and_remove_are_idempotent() -> None:
    log = logger_named("orders.idem")
    logs = capture_logs("orders.*")
    collector = Collector()

    wrapture.add_sink(collector)
    try:
        logs.apply()
        logs.apply()
        log.warning("once only")
        logs.remove()
        logs.remove()
    finally:
        wrapture.remove_sink(collector)

    assert len(collector.exited) == 1


# ---------------------------------------------------------------------------
# rendering by the printer, serialisation
# ---------------------------------------------------------------------------


def test_the_printer_prints_a_log_event_as_one_line() -> None:
    log = logger_named("orders.printer")
    logs = capture_logs("orders.*", level="ERROR")
    stream = io.StringIO()
    printer = Printer(stream)

    wrapture.add_sink(printer)
    logs.apply()
    try:
        try:
            raise ValueError("bad")
        except ValueError:
            log.exception("failed\nbadly")
    finally:
        logs.remove()
        wrapture.remove_sink(printer)

    assert stream.getvalue() == (
        "log orders.printer ERROR 'failed\\nbadly' !! ValueError\n"
    )


def test_the_serialised_record_carries_the_log_fields() -> None:
    log = logger_named("orders.jsonl")
    logs = capture_logs("orders.*")

    with timeline(logs):
        log.warning("to disk")
        event = logs.events.first

    record = _event_record(event)

    assert record["kind"] == "log"
    assert record["path"] == "orders.jsonl"
    assert record["data"]["message"] == "to disk"
    assert record["data"]["levelno"] == logging.WARNING
    assert record["duration"] == 0.0


# ---------------------------------------------------------------------------
# query-side sugar
# ---------------------------------------------------------------------------


def test_at_level_means_at_least_this_severe() -> None:
    log = logger_named("orders.sugar")
    logs = capture_logs("orders.*", level="DEBUG")

    with timeline(logs):
        log.info("routine")
        log.warning("trouble")
        log.error("worse")

        assert logs.events.at_level("WARNING").count == 2
        assert logs.events.at_level(logging.ERROR).count == 1


def test_message_filters_match_and_negate() -> None:
    log = logger_named("orders.messages")
    logs = capture_logs("orders.*")

    with timeline(logs):
        log.warning("healthcheck ok")
        log.warning("card declined")

        logs.events.with_message("*declined*").assert_once()
        logs.events.without_message("healthcheck*").assert_once()
        assert (
            logs.events.without_message("healthcheck*").first.data["message"]
            == "card declined"
        )


def test_log_sugar_never_matches_other_kinds() -> None:
    class Gateway:
        def charge(self, amount: int) -> int:
            return amount

    charge = binding(Gateway, "charge")
    gateway = Gateway()

    with timeline(charge):
        gateway.charge(500)

        assert charge.events.at_level("DEBUG").count == 0
        assert charge.events.with_message("*").count == 0
        assert charge.events.without_message("x*").count == 0


# ---------------------------------------------------------------------------
# configuration from a file
# ---------------------------------------------------------------------------


def test_a_log_entry_in_a_config_file_captures(tmp_path: Path) -> None:
    source = tmp_path / "wrapture.toml"
    source.write_text(
        '[[log]]\nname = "orders.cfg*"\nlevel = "INFO"\nexclude_message = "*secret*"\n'
    )

    log = logger_named("orders.cfg")
    collector = Collector()

    applied = wrapture.load_config(str(source)).apply()
    wrapture.add_sink(collector)
    try:
        log.info("configured capture")
        log.info("a secret thing")
        log.debug("below the threshold")

        assert len(applied.captures) == 1
        assert "orders.cfg*" in applied.report()
    finally:
        wrapture.remove_sink(collector)
        applied.revert()

    # Reverting removes the capture: nothing further records.

    wrapture.add_sink(collector)
    try:
        log.info("after revert")
    finally:
        wrapture.remove_sink(collector)

    assert [e.data["message"] for e in collector.exited] == ["configured capture"]


def test_config_suspend_covers_log_captures(tmp_path: Path) -> None:
    source = tmp_path / "wrapture.toml"
    source.write_text('[[log]]\nname = "orders.cfgsuspend*"\n')

    log = logger_named("orders.cfgsuspend")
    collector = Collector()

    applied = wrapture.load_config(str(source)).apply()
    wrapture.add_sink(collector)
    try:
        applied.suspend()
        log.warning("while suspended")

        applied.resume()
        log.warning("after resume")
    finally:
        wrapture.remove_sink(collector)
        applied.revert()

    assert [e.data["message"] for e in collector.exited] == ["after resume"]


def test_a_bad_log_entry_fails_the_config(tmp_path: Path) -> None:
    bad_level = tmp_path / "level.toml"
    bad_level.write_text('[[log]]\nlevel = "LOUD"\n')

    with pytest.raises(wrapture.ConfigError, match="unknown logging level"):
        wrapture.load_config(str(bad_level))

    bad_key = tmp_path / "key.toml"
    bad_key.write_text('[[log]]\nlogger = "orders.*"\n')

    with pytest.raises(wrapture.ConfigError, match="unknown keys"):
        wrapture.load_config(str(bad_key))
