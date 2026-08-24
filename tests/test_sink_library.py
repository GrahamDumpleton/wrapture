"""Tests for the sink library: the counting sinks and the combinators.

Counter and Aggregate retain nothing and declare "none" capture, so
they are cheap enough to leave on for a whole suite or process. The
combinators compose sinks: Fanout duplicates, Filter and Depth narrow,
Sample keeps a random fraction of whole trees, deciding at the root so
children are never orphaned.
"""

import warnings
from collections.abc import Generator
from contextlib import contextmanager

import pytest
from wrapt import MISSING

from wrapture import (
    Aggregate,
    Counter,
    Depth,
    Event,
    Fanout,
    Filter,
    Sample,
    Sink,
    binding,
)
from wrapture.capture import SUMMARY
from wrapture.sinks import _scoped_sinks


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"

    def refund(self, amount: int) -> str:
        raise TimeoutError("gateway offline")


class Processor:
    def process(self) -> str:
        return Gateway().charge(500)


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

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.notified]


@contextmanager
def listening(*sinks: Sink) -> Generator[None]:
    token = _scoped_sinks.set(_scoped_sinks.get() + sinks)
    try:
        yield
    finally:
        _scoped_sinks.reset(token)


# ---------------------------------------------------------------------------
# the counting sinks
# ---------------------------------------------------------------------------


def test_counter_counts_operations_and_retains_nothing() -> None:
    counter = Counter()
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")

    with charge, refund, listening(counter):
        gateway = Gateway()
        gateway.charge(1)
        gateway.charge(2)

        with pytest.raises(TimeoutError):
            gateway.refund(3)

    # Failures count too: the count is of operations beginning.

    assert counter.count == 3
    assert repr(counter) == "<Counter 'counter': 3>"
    assert Counter.capture_args == "none"
    assert Counter.capture_result == "none"


def test_aggregate_collects_per_path_stats() -> None:
    aggregate = Aggregate()
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")

    with charge, refund, listening(aggregate):
        gateway = Gateway()
        gateway.charge(1)
        gateway.charge(2)

        with pytest.raises(TimeoutError):
            gateway.refund(3)

    stats = aggregate.stats
    charged = stats[f"{Gateway.__module__}:Gateway.charge"]
    refunded = stats[f"{Gateway.__module__}:Gateway.refund"]

    assert charged.count == 2
    assert charged.min is not None and charged.max is not None
    assert 0.0 <= charged.min <= charged.max <= charged.total

    # A raising operation still has a duration, folded in on_error.

    assert refunded.count == 1
    assert refunded.total > 0.0
    assert refunded.min == refunded.max == refunded.total


def test_aggregate_computes_self_time_from_parent_links() -> None:
    aggregate = Aggregate()
    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")

    with process, charge, listening(aggregate):
        Processor().process()
        Processor().process()

    stats = aggregate.stats
    processed = stats[f"{Processor.__module__}:Processor.process"]
    charged = stats[f"{Gateway.__module__}:Gateway.charge"]

    # A leaf keeps all of its time; the parent's self time excludes
    # exactly what its child deposited while in flight.

    assert charged.self_total == charged.total
    assert processed.self_total == pytest.approx(processed.total - charged.total)
    assert processed.self_total < processed.total

    # Nothing stays behind once every event has closed.

    assert aggregate._pending == {}


# ---------------------------------------------------------------------------
# Fanout
# ---------------------------------------------------------------------------


class Summarizing(Sink):
    """A sink declaring the streaming-shaped capture requirement."""

    capture_args = "summary"
    capture_result = "summary"


def test_fanout_delivers_to_every_inner_sink() -> None:
    first = Probe()
    second = Probe()
    charge = binding(Gateway, "charge")

    with charge, listening(Fanout(first, second)):
        Gateway().charge(500)

    assert first.kinds() == ["enter", "exit"]
    assert second.kinds() == ["enter", "exit"]


def test_fanout_declares_the_highest_inner_capture_level() -> None:
    fanout = Fanout(Counter(), Summarizing())

    # Declarations are resolved to numeric levels at construction, so
    # capture negotiation sees the strictest inner requirement.

    assert fanout.capture_args == SUMMARY
    assert fanout.capture_result == SUMMARY


def test_a_broken_inner_sink_is_isolated_inside_fanout() -> None:
    class Exploding(Sink):
        def on_enter(self, event: Event) -> None:
            raise RuntimeError("broken inner sink")

    exploding = Exploding()
    probe = Probe()
    fanout = Fanout(exploding, probe)
    charge = binding(Gateway, "charge")

    with charge, listening(fanout):
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            assert Gateway().charge(500) == "ch_500"

    # The count lands on the sink that broke, not on the Fanout, and
    # the sibling after it still heard everything.

    assert exploding.errors == 1
    assert fanout.errors == 0
    assert probe.kinds() == ["enter", "exit"]


def test_fanout_flushes_every_inner_sink() -> None:
    flushed: list[int] = []

    class Buffered(Sink):
        def __init__(self, tag: int) -> None:
            self._tag = tag

        def flush(self) -> None:
            flushed.append(self._tag)

    Fanout(Buffered(1), Buffered(2)).flush()

    assert flushed == [1, 2]


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def test_filter_forwards_only_accepted_events() -> None:
    probe = Probe()
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")

    narrowed = Filter(
        lambda event: event.path == "test_sink_library:Gateway.charge", probe
    )

    with charge, refund, listening(narrowed):
        gateway = Gateway()
        gateway.charge(1)

        with pytest.raises(TimeoutError):
            gateway.refund(2)

    # The refund produced neither an enter nor an error downstream.

    assert probe.kinds() == ["enter", "exit"]
    assert probe.notified[0][1].path == "test_sink_library:Gateway.charge"


def test_the_filter_decision_is_made_at_enter_and_sticks() -> None:
    probe = Probe()
    charge = binding(Gateway, "charge")

    # True for every event at enter time (no result yet), false by exit
    # time: the exit is still forwarded, because the decision is made
    # once, so the inner sink sees properly paired notifications.

    sticky = Filter(lambda event: event.result is MISSING, probe)

    with charge, listening(sticky):
        Gateway().charge(500)

    assert probe.kinds() == ["enter", "exit"]


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------


def test_depth_forwards_only_the_top_levels() -> None:
    probe = Probe()
    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")

    with process, charge, listening(Depth(1, probe)):
        Processor().process()

    # Only the root is heard, with its pairing intact; the nested
    # charge at depth 1 is cut.

    assert probe.kinds() == ["enter", "exit"]
    assert [event.path for _, event in probe.notified] == [
        "test_sink_library:Processor.process",
        "test_sink_library:Processor.process",
    ]


def test_depth_rejects_a_nonpositive_cut() -> None:
    with pytest.raises(ValueError, match="max_depth must be"):
        Depth(0, Probe())


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------


def test_sample_rejects_a_rate_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError, match="rate must be"):
        Sample(1.5, Probe())

    with pytest.raises(ValueError, match="rate must be"):
        Sample(-0.1, Probe())


def test_a_kept_tree_flows_through_whole() -> None:
    probe = Probe()
    sample = Sample(0.5, probe)
    sample._random = lambda: 0.0

    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")

    with process, charge, listening(sample):
        Processor().process()

    assert [(kind, event.path) for kind, event in probe.notified] == [
        ("enter", "test_sink_library:Processor.process"),
        ("enter", "test_sink_library:Gateway.charge"),
        ("exit", "test_sink_library:Gateway.charge"),
        ("exit", "test_sink_library:Processor.process"),
    ]


def test_the_sampling_decision_is_made_at_the_root_only() -> None:
    probe = Probe()
    sample = Sample(0.5, probe)

    # The root draw rejects; a second draw would accept. The child must
    # never trigger that second draw: it inherits the root's decision,
    # so nothing is forwarded and one draw remains unconsumed.

    draws = iter([0.99, 0.0])
    sample._random = lambda: next(draws)

    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")

    with process, charge, listening(sample):
        Processor().process()

    assert probe.notified == []
    assert next(draws) == 0.0


def test_kept_trees_leave_no_bookkeeping_behind() -> None:
    probe = Probe()
    sample = Sample(0.5, probe)
    sample._random = lambda: 0.0

    charge = binding(Gateway, "charge")

    with charge, listening(sample):
        Gateway().charge(1)
        Gateway().charge(2)

    # Kept entries are dropped again as each event closes, so the
    # per-tree state does not accumulate over a long run.

    assert sample._kept == set()
