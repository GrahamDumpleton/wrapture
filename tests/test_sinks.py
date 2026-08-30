"""Tests for the sink layer: the protocol, the registry, and the gate.

The recording gate is "is anything listening", not "is there a
timeline". These tests register sinks directly on the two registry
tiers and record with no timeline anywhere, pin the enter/exit/error
pairing, the effective capture level across several sinks, and the
fast path a bound but unmonitored call takes.
"""

import importlib
import io
import re
import threading
import time
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from wrapt import MISSING

from wrapture import (
    Event,
    Printer,
    Sink,
    SinkErrorWarning,
    add_sink,
    binding,
    flush_sinks,
    remove_sink,
    timeline,
)
from wrapture import sinks as sinks_module
from wrapture.sinks import _active_sinks, _scoped_sinks

# The package exports binding() and bindings() from these submodules, so
# the submodule attributes on the package are shadowed by the functions;
# resolve the module objects for monkeypatching.

_bindings_module = importlib.import_module("wrapture.bindings")
_attributes_module = importlib.import_module("wrapture.attributes")


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"

    def refund(self, amount: int) -> str:
        raise TimeoutError("gateway offline")


class Ledger:
    def record(self, entries: list[str]) -> int:
        return len(entries)


class Feed:
    def stream(self, count: int) -> Generator[int, None, None]:
        yield from range(count)


class Model:
    @property
    def total(self) -> int:
        return 42


class Probe(Sink):
    """A sink that notes every notification it receives."""

    def __init__(self) -> None:
        self.notified: list[tuple[str, Event]] = []

    def on_enter(self, event: Event) -> None:
        self.notified.append(("enter", event))

    def on_exit(self, event: Event) -> None:
        self.notified.append(("exit", event))

    def on_error(self, event: Event) -> None:
        self.notified.append(("error", event))


@contextmanager
def listening(*sinks: Sink) -> Generator[None]:
    # Activate sinks on the scoped tier directly, without a timeline:
    # the public scoped route is the timeline, but the gate itself only
    # asks whether anything is listening.

    token = _scoped_sinks.set(_scoped_sinks.get() + sinks)
    try:
        yield
    finally:
        _scoped_sinks.reset(token)


# ---------------------------------------------------------------------------
# the protocol: enter, exit, error
# ---------------------------------------------------------------------------


def test_a_call_notifies_enter_then_exit_with_no_timeline() -> None:
    probe = Probe()
    charge = binding(Gateway, "charge")

    with charge, listening(probe):
        Gateway().charge(500)

    kinds = [kind for kind, _ in probe.notified]
    assert kinds == ["enter", "exit"]

    # Both notifications carry the same live event, already carrying
    # its identity, and by exit time its outcome.

    entered, exited = (event for _, event in probe.notified)
    assert entered is exited
    assert entered.seq > 0
    assert entered.result == "ch_500"


def test_enter_is_delivered_before_the_operation_runs() -> None:
    results_at_enter: list[Any] = []

    class Early(Sink):
        def on_enter(self, event: Event) -> None:
            results_at_enter.append(event.result)

    charge = binding(Gateway, "charge")

    with charge, listening(Early()):
        Gateway().charge(500)

    assert results_at_enter == [MISSING]


def test_a_raising_call_notifies_error() -> None:
    probe = Probe()
    refund = binding(Gateway, "refund")

    with refund, listening(probe):
        try:
            Gateway().refund(100)
        except TimeoutError:
            pass

    kinds = [kind for kind, _ in probe.notified]
    assert kinds == ["enter", "error"]
    assert isinstance(probe.notified[-1][1].exception, TimeoutError)


def test_a_generator_exits_when_iteration_finishes() -> None:
    probe = Probe()
    stream = binding(Feed, "stream")

    with stream, listening(probe):
        items = list(Feed().stream(3))

    assert items == [0, 1, 2]

    kinds = [kind for kind, _ in probe.notified]
    assert kinds == ["enter", "exit"]
    assert probe.notified[-1][1].items == 3


def test_attribute_events_pair_enter_and_exit_too() -> None:
    probe = Probe()
    total = binding(Model, "total")

    with total, listening(probe):
        assert Model().total == 42

    kinds = [kind for kind, _ in probe.notified]
    assert kinds == ["enter", "exit"]
    assert probe.notified[-1][1].kind == "get"


def test_process_sinks_hear_events_alongside_scoped_ones() -> None:
    process_probe = Probe()
    scoped_probe = Probe()
    charge = binding(Gateway, "charge")

    assert add_sink(process_probe) is process_probe
    try:
        with charge, listening(scoped_probe):
            assert _active_sinks() == (process_probe, scoped_probe)
            Gateway().charge(500)
    finally:
        remove_sink(process_probe)

    assert [kind for kind, _ in process_probe.notified] == ["enter", "exit"]
    assert [kind for kind, _ in scoped_probe.notified] == ["enter", "exit"]
    assert sinks_module._process_sinks == ()


def test_removing_an_unregistered_sink_raises() -> None:
    with pytest.raises(ValueError, match="not a registered process sink"):
        remove_sink(Probe())


class Worker:
    def outer(self) -> str:
        return self.inner()

    def inner(self) -> str:
        return "done"


def test_a_process_sink_hears_worker_threads_with_nesting_intact() -> None:
    probe = Probe()
    outer = binding(Worker, "outer")
    inner = binding(Worker, "inner")

    with outer, inner:
        add_sink(probe)
        try:
            thread = threading.Thread(target=Worker().outer)
            thread.start()
            thread.join()
        finally:
            remove_sink(probe)

    entered = [event for kind, event in probe.notified if kind == "enter"]
    assert [event.path for event in entered] == [
        "test_sinks:Worker.outer",
        "test_sinks:Worker.inner",
    ]

    # The thread carried no timeline context, but the process tier is a
    # plain list visible everywhere, and nesting within the thread is
    # intact.

    outer_event, inner_event = entered
    assert inner_event.parent_id == outer_event.seq
    assert inner_event.depth == 1


# ---------------------------------------------------------------------------
# the sink says what it needs
# ---------------------------------------------------------------------------


class Summarizing(Sink):
    """A sink declaring the streaming-shaped capture requirement."""

    capture_args = "summary"
    capture_result = "summary"


def test_the_highest_declared_capture_level_wins() -> None:
    record = binding(Ledger, "record")
    entries = ["a", "b"]

    with record, timeline() as tape, listening(Summarizing()):
        Ledger().record(entries)

        # The tape declares "reference" but the summarizing sink needs
        # more, so values are captured at "summary" for both: mutating
        # the list afterwards does not change the record.

        entries.append("c")

        event = tape.all[0]
        assert event.arguments == {"entries": "<list ['a', 'b']>"}
        assert event.result == 2


def test_a_binding_override_beats_every_sink_declaration() -> None:
    record = binding(Ledger, "record", capture="none")

    with record, timeline() as tape, listening(Summarizing()):
        Ledger().record(["a"])

        event = tape.all[0]
        assert event.arguments is None
        assert event.result is MISSING


# ---------------------------------------------------------------------------
# the fast path: nothing listening, nothing constructed
# ---------------------------------------------------------------------------


def test_no_event_is_constructed_when_nothing_listens(
    monkeypatch: Any,
) -> None:
    def bomb(*args: Any, **kwargs: Any) -> Event:
        raise AssertionError("an Event was constructed on the fast path")

    monkeypatch.setattr(_bindings_module, "Event", bomb)

    charge = binding(Gateway, "charge")
    with charge:
        assert Gateway().charge(500) == "ch_500"


def test_no_attribute_event_is_constructed_when_nothing_listens(
    monkeypatch: Any,
) -> None:
    def bomb(*args: Any, **kwargs: Any) -> Event:
        raise AssertionError("an Event was constructed on the fast path")

    monkeypatch.setattr(_attributes_module, "Event", bomb)

    total = binding(Model, "total")
    with total:
        assert Model().total == 42


def test_bound_but_unmonitored_calls_stay_cheap() -> None:
    # A regression canary, not a benchmark. The real fast-path cost is
    # under a microsecond; the threshold is deliberately far above it
    # so only a gross regression, such as recording work leaking onto
    # the fast path, can trip it on a slow CI machine.

    gateway = Gateway()
    charge = binding(Gateway, "charge")

    with charge:
        iterations = 5000
        started = time.perf_counter()

        for _ in range(iterations):
            gateway.charge(500)

        elapsed = time.perf_counter() - started

    assert elapsed / iterations < 25e-6


# ---------------------------------------------------------------------------
# sink errors never propagate
# ---------------------------------------------------------------------------


class Exploding(Sink):
    """A sink that raises from every notification."""

    def on_enter(self, event: Event) -> None:
        raise RuntimeError("broken sink")

    def on_exit(self, event: Event) -> None:
        raise RuntimeError("broken sink")


def test_a_broken_sink_never_breaks_the_observed_call() -> None:
    exploding = Exploding()
    probe = Probe()
    charge = binding(Gateway, "charge")

    with charge, listening(exploding, probe):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            assert Gateway().charge(500) == "ch_500"
            assert Gateway().charge(501) == "ch_501"

    # The failures are counted on the sink and warned about exactly
    # once, and the sink behind it in the delivery order is unaffected.

    assert exploding.errors == 4
    assert [w for w in caught if w.category is SinkErrorWarning] != []
    assert len([w for w in caught if w.category is SinkErrorWarning]) == 1
    assert [kind for kind, _ in probe.notified] == ["enter", "exit"] * 2


def test_process_sinks_are_flushed_at_shutdown_with_isolation() -> None:
    flushed: list[bool] = []

    class Buffered(Sink):
        def flush(self) -> None:
            flushed.append(True)

    class Unflushable(Sink):
        def flush(self) -> None:
            raise RuntimeError("cannot flush")

    broken = add_sink(Unflushable())
    buffered = add_sink(Buffered())

    # flush_sinks() is the sink half of shutdown(): the broken sink is
    # counted and skipped, the one registered after it still flushes,
    # and repeating is safe.

    try:
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            flush_sinks()
            flush_sinks()
    finally:
        remove_sink(broken)
        remove_sink(buffered)

    assert flushed == [True, True]
    assert broken.errors == 2


# ---------------------------------------------------------------------------
# the Printer sink
# ---------------------------------------------------------------------------


class Processor:
    def process(self) -> str:
        try:
            Gateway().refund(1)
        except TimeoutError:
            pass
        return "ok"


def test_printer_prints_the_call_and_its_outcome() -> None:
    output = io.StringIO()
    charge = binding(Gateway, "charge")

    with charge, listening(Printer(output, timing=False)):
        Gateway().charge(500)

    assert output.getvalue() == (
        "test_sinks:Gateway.charge(amount=500)\ntest_sinks:Gateway.charge -> 'ch_500'\n"
    )


def test_printer_indents_by_depth_and_marks_errors() -> None:
    output = io.StringIO()
    process = binding(Processor, "process")
    refund = binding(Gateway, "refund")

    with process, refund, listening(Printer(output, timing=False)):
        Processor().process()

    assert output.getvalue() == (
        "test_sinks:Processor.process()\n"
        "  test_sinks:Gateway.refund(amount=1)\n"
        "  test_sinks:Gateway.refund !! TimeoutError\n"
        "test_sinks:Processor.process -> 'ok'\n"
    )


def test_printer_skips_the_outcome_line_when_nothing_was_captured() -> None:
    output = io.StringIO()
    charge = binding(Gateway, "charge", capture="none")

    with charge, listening(Printer(output, timing=False)):
        Gateway().charge(500)

    assert output.getvalue() == "test_sinks:Gateway.charge()\n"


def test_printer_times_closing_lines_by_default() -> None:
    output = io.StringIO()
    process = binding(Processor, "process")
    refund = binding(Gateway, "refund")

    with process, refund, listening(Printer(output)):
        Processor().process()

    lines = output.getvalue().splitlines()

    assert lines[0] == "test_sinks:Processor.process()"
    assert lines[1] == "  test_sinks:Gateway.refund(amount=1)"
    assert re.fullmatch(
        r"  test_sinks:Gateway.refund !! TimeoutError \[\d+us\]", lines[2]
    )
    assert re.fullmatch(
        r"test_sinks:Processor.process -> 'ok' \[[\d.]+(us|ms|s)\]", lines[3]
    )


def test_printer_still_closes_a_timed_line_with_no_captured_result() -> None:
    output = io.StringIO()
    charge = binding(Gateway, "charge", capture="none")

    with charge, listening(Printer(output)):
        Gateway().charge(500)

    lines = output.getvalue().splitlines()

    assert lines[0] == "test_sinks:Gateway.charge()"
    assert re.fullmatch(r"test_sinks:Gateway.charge \[\d+us\]", lines[1])


class Streamer:
    def stream(self) -> Generator[int]:
        yield 1
        yield 2
        yield 3


def test_printer_shows_the_body_split_for_a_streamed_result() -> None:
    output = io.StringIO()
    stream = binding(Streamer, "stream")

    with stream, listening(Printer(output)):
        list(Streamer().stream())

    closing = output.getvalue().splitlines()[-1]

    unit = r"[\d.]+(us|ms|s)"

    assert re.fullmatch(
        rf"test_sinks:Streamer.stream -> None \[{unit}, body {unit} over 3 items\]",
        closing,
    )


def test_printer_timestamps_opening_lines_and_pads_the_rest() -> None:
    output = io.StringIO()
    process = binding(Processor, "process")
    refund = binding(Gateway, "refund")

    with process, refund, listening(Printer(output, timing=False, timestamps=True)):
        Processor().process()

    lines = output.getvalue().splitlines()
    clock = r"\d\d:\d\d:\d\d\.\d\d\d"

    assert re.fullmatch(clock + r" test_sinks:Processor.process\(\)", lines[0])
    assert re.fullmatch(clock + r"   test_sinks:Gateway.refund\(amount=1\)", lines[1])
    assert lines[2] == " " * 13 + "  test_sinks:Gateway.refund !! TimeoutError"
    assert lines[3] == " " * 13 + "test_sinks:Processor.process -> 'ok'"


def test_printer_appends_to_a_file_at_path(tmp_path: Path) -> None:
    target = tmp_path / "logs" / "trace.log"
    target.parent.mkdir()
    target.write_text("earlier\n")

    printer = Printer(path=target, timing=False)
    charge = binding(Gateway, "charge")

    with charge, listening(printer):
        Gateway().charge(500)

    printer.close()
    printer.close()

    assert target.read_text() == (
        "earlier\ntest_sinks:Gateway.charge(amount=500)\n"
        "test_sinks:Gateway.charge -> 'ch_500'\n"
    )

    # Lines after close are dropped rather than reopening the file.

    with charge, listening(printer):
        Gateway().charge(1)

    assert target.read_text().count("test_sinks:Gateway.charge(") == 1


def test_printer_opens_the_file_lazily(tmp_path: Path) -> None:
    target = tmp_path / "trace.log"

    printer = Printer(path=target)

    assert not target.exists()

    printer.flush()
    printer.close()

    assert not target.exists()


def test_printer_path_is_a_template_and_reopen_moves_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wrapture.outputs

    clock = [1_700_000_000.0]
    monkeypatch.setattr(wrapture.outputs, "_now", lambda: clock[0])

    printer = Printer(path=tmp_path / "logs" / "{name}-{epoch}.log", timing=False)
    charge = binding(Gateway, "charge")

    assert repr(printer).startswith("Printer(path=")
    assert printer.path is None

    with charge, listening(printer):
        Gateway().charge(1)
        clock[0] += 60
        printer.reopen()
        Gateway().charge(2)

    printer.close()

    first = tmp_path / "logs" / "printer-1700000000.log"
    second = tmp_path / "logs" / "printer-1700000060.log"

    assert first.read_text().count("test_sinks:Gateway.charge(") == 1
    assert second.read_text().count("test_sinks:Gateway.charge(") == 1
    assert printer.path == str(second)


def test_printer_rotate_needs_a_path() -> None:
    with pytest.raises(ValueError, match="need a path"):
        Printer(rotate="1h")


def test_printer_refuses_both_stream_and_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either stream or path"):
        Printer(io.StringIO(), path=tmp_path / "trace.log")


def test_printer_defaults_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    charge = binding(Gateway, "charge")

    with charge, listening(Printer()):
        Gateway().charge(500)

    captured = capsys.readouterr()
    assert "test_sinks:Gateway.charge(amount=500)" in captured.err
    assert captured.out == ""
