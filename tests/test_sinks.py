"""Tests for the sink layer: the protocol, the registry, and the gate.

The recording gate is "is anything listening", not "is there a
timeline". These tests register sinks directly on the two registry
tiers and record with no timeline anywhere, pin the enter/exit/error
pairing, the effective capture level across several sinks, and the
fast path a bound but unmonitored call takes.
"""

import importlib
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from wrapt import MISSING

from wrapture import Event, binding, timeline
from wrapture.sinks import Sink, _active_sinks, _process_sinks, _scoped_sinks

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

    _process_sinks.append(process_probe)
    try:
        with charge, listening(scoped_probe):
            assert _active_sinks() == (process_probe, scoped_probe)
            Gateway().charge(500)
    finally:
        _process_sinks.remove(process_probe)

    assert [kind for kind, _ in process_probe.notified] == ["enter", "exit"]
    assert [kind for kind, _ in scoped_probe.notified] == ["enter", "exit"]


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
