"""Tests for the tape, the ambient recording state, and timeline scoping.

The wrappers do not emit events yet, so these tests drive the tape and
the nesting helpers by hand: that is the contract this layer provides to
the recording wiring built on top of it.
"""

import asyncio
from typing import Any

import pytest

from wrapture import (
    AlreadyAppliedError,
    Event,
    ExpectationNotMetError,
    Tape,
    binding,
    bindings,
    timeline,
)
from wrapture.events import _format_time
from wrapture.sinks import _active_sinks, _record_event
from wrapture.timeline import _current_tape, _pop, _push, _stack


class Gateway:
    def charge(self, amount: int) -> dict[str, Any]:
        return {"id": f"ch_{amount}", "amount": amount}

    def refund(self, amount: int) -> dict[str, Any]:
        return {"id": f"rf_{amount}", "amount": amount}


class Ledger:
    def record(self, entry: dict[str, Any]) -> None:
        pass


# ---------------------------------------------------------------------------
# the tape
# ---------------------------------------------------------------------------


def test_recorded_events_get_increasing_sequence_numbers() -> None:
    tape = Tape()
    first = _record_event(Event("call", "a"), (tape,))
    second = _record_event(Event("call", "b"), (tape,))

    assert second.seq == first.seq + 1
    assert tape.all == [first, second]


def test_tape_all_returns_a_copy() -> None:
    tape = Tape()
    event = _record_event(Event("call", "a"), (tape,))

    tape.all.clear()
    assert tape.all == [event]


# ---------------------------------------------------------------------------
# nesting: push and pop
# ---------------------------------------------------------------------------


def test_push_assigns_parent_id_and_depth() -> None:
    # Events are recorded before they are pushed, so a pushed event
    # always carries the seq its children link to.

    tape = Tape()
    outer = _record_event(Event("call", "outer"), (tape,))
    inner = _record_event(Event("call", "inner"), (tape,))

    outer_token = _push(outer)
    assert outer.depth == 0
    assert outer.parent_id is None

    inner_token = _push(inner)
    assert inner.depth == 1
    assert inner.parent_id == outer.seq

    # Popping the inner event makes the next push a sibling, not a
    # grandchild.

    _pop(inner_token)
    sibling = _record_event(Event("call", "sibling"), (tape,))
    sibling_token = _push(sibling)

    assert sibling.depth == 1
    assert sibling.parent_id == outer.seq
    assert tape.children_of(outer) == [inner, sibling]
    assert tape.parent_of(sibling) is outer

    _pop(sibling_token)
    _pop(outer_token)
    assert _stack.get() == ()


# ---------------------------------------------------------------------------
# the timeline scope
# ---------------------------------------------------------------------------


def test_nothing_listens_outside_a_timeline() -> None:
    assert _active_sinks() == ()
    assert _current_tape() is None

    with timeline() as tape:
        assert isinstance(tape, Tape)
        assert _active_sinks() == (tape,)
        assert _current_tape() is tape

    assert _active_sinks() == ()
    assert _current_tape() is None


def test_timeline_applies_bindings_on_entry_and_removes_on_exit() -> None:
    charge = binding(Gateway, "charge").on_call.returns({"id": "stub"})

    with timeline(charge):
        assert charge.active
        assert Gateway().charge(1) == {"id": "stub"}

    assert not charge.active
    assert Gateway().charge(1) == {"id": "ch_1", "amount": 1}


def test_timeline_accepts_groups_and_iterables() -> None:
    group = bindings(
        charge=binding(Gateway, "charge"), record=binding(Ledger, "record")
    )
    extra = binding(Gateway, "refund")

    with timeline(group, [extra]):
        assert group.charge.active
        assert group.record.active
        assert extra.active

    assert not group.charge.active
    assert not group.record.active
    assert not extra.active


def test_partial_application_rolls_back_and_restores_state() -> None:
    first = binding(Gateway, "charge")
    second = binding(Gateway, "refund").apply()

    try:
        with pytest.raises(AlreadyAppliedError):
            with timeline(first, second):
                pass

        assert not first.active
        assert _active_sinks() == ()
        assert _stack.get() == ()
    finally:
        second.remove()


def test_timeline_reuse_accumulates_on_the_same_tape() -> None:
    scope = timeline()

    with scope as tape:
        _record_event(Event("call", "a"), _active_sinks())

    with scope as again:
        assert again is tape
        _record_event(Event("call", "b"), _active_sinks())

    assert [event.path for event in tape.all] == ["a", "b"]

    first, second = tape.all
    assert second.seq == first.seq + 1


def test_the_tape_closes_at_exit_and_reopens_on_reuse() -> None:
    scope = timeline()

    with scope as tape:
        assert not tape.closed
        _record_event(Event("call", "a"), _active_sinks())

    # Closed: a late arrival is discarded and counted, not appended,
    # so a tape already asserted on cannot change shape.

    assert tape.closed
    _record_event(Event("call", "late"), (tape,))

    assert [event.path for event in tape.all] == ["a"]
    assert tape.discarded == 1

    # Reuse reopens the tape; the discard count is history and stays.

    with scope as again:
        assert not again.closed
        _record_event(Event("call", "b"), _active_sinks())

    assert [event.path for event in tape.all] == ["a", "b"]
    assert tape.discarded == 1


def test_self_time_subtracts_observed_children() -> None:
    tape = Tape()
    outer = _record_event(Event("call", "outer"), (tape,))
    inner = _record_event(Event("call", "inner", parent_id=outer.seq, depth=1), (tape,))

    outer.duration = 0.010
    inner.duration = 0.004

    assert tape.self_time(outer) == pytest.approx(0.006)
    assert tape.self_time(inner) == 0.004
    assert tape.self_time(Event("call", "open")) is None


def test_self_time_uses_body_time_for_generators() -> None:
    tape = Tape()
    stream = _record_event(Event("call", "stream"), (tape,))
    fetch = _record_event(
        Event("call", "fetch", parent_id=stream.seq, depth=1), (tape,)
    )

    # The generator's wall duration includes the consumer's time
    # between yields; only its body time is its own to spend.

    stream.duration = 0.100
    stream.body_duration = 0.030
    fetch.duration = 0.010

    assert tape.self_time(stream) == pytest.approx(0.020)


def test_tree_with_times_shows_durations_and_self_time() -> None:
    tape = Tape()
    outer = _record_event(Event("call", "outer"), (tape,))
    inner = _record_event(Event("call", "inner", parent_id=outer.seq, depth=1), (tape,))

    outer.duration = 0.010
    inner.duration = 0.004

    rendered = tape.tree(times=True)
    assert "[10.0ms, self 6.0ms]" in rendered
    assert "[4.0ms]" in rendered

    # The default rendering stays free of timing noise.

    assert "ms" not in tape.tree()


def test_time_formatting_adapts_units() -> None:
    assert _format_time(2.5) == "2.50s"
    assert _format_time(0.0123) == "12.3ms"
    assert _format_time(0.000045) == "45us"


def test_tape_repr_shows_the_event_and_discard_counts() -> None:
    tape = Tape()
    assert repr(tape) == "<Tape: 0 events>"

    event = _record_event(Event("call", "a"), (tape,))
    assert repr(tape) == "<Tape: 1 event, 1 pending>"
    assert tape.pending == 1

    event.duration = 0.0
    assert repr(tape) == "<Tape: 1 event>"
    assert tape.pending == 0

    tape._close()
    _record_event(Event("call", "late"), (tape,))
    assert repr(tape) == "<Tape: 1 event, 1 discarded after close>"

    _record_event(Event("call", "b"), (tape,))  # discarded too: closed
    tape._open()
    _record_event(Event("call", "c"), (tape,))
    assert repr(tape) == "<Tape: 2 events, 1 pending, 2 discarded after close>"


def test_reentering_an_active_timeline_raises() -> None:
    scope = timeline()

    with scope:
        with pytest.raises(RuntimeError):
            with scope:
                pass


def test_nested_timelines_restore_the_outer_scope() -> None:
    with timeline() as outer:
        in_progress = Event("call", "outer.work")
        token = _push(in_progress)

        # An inner timeline starts with a fresh stack, so its events do
        # not nest under the outer scope's in-progress call, and exiting
        # restores both the outer sinks and the outer stack. While both
        # scopes are open, both tapes listen: the inner one is pushed
        # onto the scoped sinks, not swapped in.

        with timeline() as inner:
            assert _active_sinks() == (outer, inner)
            assert _current_tape() is inner
            assert _stack.get() == ()

        assert _active_sinks() == (outer,)
        assert _stack.get() == (in_progress,)
        _pop(token)


def test_a_nested_timeline_records_onto_the_outer_tape_too() -> None:
    charge = binding(Gateway, "charge")

    with timeline() as outer:
        with timeline(charge) as inner:
            Gateway().charge(500)

    # Scoped sinks stack rather than shadow: the outer scope stays
    # listening, so both tapes hold the event.

    assert [event.path for event in inner.all] == ["test_timeline:Gateway.charge"]
    assert [event.path for event in outer.all] == ["test_timeline:Gateway.charge"]


# ---------------------------------------------------------------------------
# tape-level views
# ---------------------------------------------------------------------------


class Processor:
    def process(self, order: str) -> dict[str, Any]:
        gateway = Gateway()
        result = gateway.charge(500)
        Ledger().record(result)
        return result


def test_roots_returns_only_top_level_events() -> None:
    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")

    with timeline(process, charge) as tape:
        Processor().process("widget")

    roots = tape.roots()

    assert [event.path for event in roots] == ["test_timeline:Processor.process"]
    assert [event.path for event in tape.all] == [
        "test_timeline:Processor.process",
        "test_timeline:Gateway.charge",
    ]


def test_tree_renders_nesting_results_and_failures() -> None:
    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund").on_call.raises(TimeoutError("down"))

    with timeline(process, charge, refund) as tape:
        Processor().process("widget")
        with pytest.raises(TimeoutError):
            Gateway().refund(100)

    lines = tape.tree().splitlines()

    assert lines[0].startswith("test_timeline:Processor.process(")
    assert lines[0].endswith("-> {'id': 'ch_500', 'amount': 500}")
    assert lines[1].startswith("  test_timeline:Gateway.charge(")
    assert lines[2] == (
        "test_timeline:Gateway.refund(amount=100)  !! TimeoutError (injected)"
    )


def test_tree_shows_a_get_event_value_once() -> None:
    class Settings:
        retries = 3

    retries = binding(Settings, "retries")

    with timeline(retries) as tape:
        _ = Settings().retries

    assert tape.tree() == (
        "get test_timeline:test_tree_shows_a_get_event_value_once"
        ".<locals>.Settings.retries -> 3"
    )


def test_tree_of_an_empty_tape_is_empty() -> None:
    with timeline() as tape:
        pass

    assert tape.tree() == ""


def test_an_event_under_an_unheard_parent_is_a_root_not_dropped() -> None:
    # A timeline entered mid-operation hears events whose parent_id
    # names an event recorded before the tape existed (a generator
    # created earlier and consumed inside the scope, say). Such an
    # event stands as a root of this view, children in tow, rather
    # than vanishing from roots() and tree().

    tape = Tape()

    orphan = Event("call", "svc:inner", label="inner")
    orphan.seq = 100
    orphan.parent_id = 41
    orphan.depth = 3

    child = Event("call", "svc:leaf", label="leaf")
    child.seq = 101
    child.parent_id = 100
    child.depth = 4

    tape.on_enter(orphan)
    tape.on_enter(child)

    assert tape.roots() == [orphan]
    assert tape.tree() == "inner()\n  leaf()"


def test_assert_order_checks_a_subsequence_not_an_exact_match() -> None:
    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")
    record = binding(Ledger, "record")

    with timeline(process, charge, record) as tape:
        Processor().process("widget")

    # Other events in between are fine; only relative order matters, and
    # a passing check chains.

    assert tape.assert_order(process, record) is tape
    tape.assert_order(charge, record)
    tape.assert_order(process, charge, record)


def test_assert_order_failure_names_where_it_stalled() -> None:
    charge = binding(Gateway, "charge")
    record = binding(Ledger, "record")

    with timeline(charge, record) as tape:
        Ledger().record({"id": "manual"})
        Gateway().charge(500)

    with pytest.raises(AssertionError) as failure:
        tape.assert_order(charge, record)

    message = str(failure.value)
    assert (
        "stalled waiting for test_timeline:Ledger.record (position 2 of 2)" in message
    )
    assert "actual timeline:" in message
    assert "test_timeline:Gateway.charge(amount=500)" in message


def test_assert_order_failure_on_a_binding_that_never_recorded() -> None:
    charge = binding(Gateway, "charge")
    record = binding(Ledger, "record")

    with timeline(charge, record) as tape:
        Gateway().charge(500)

    with pytest.raises(AssertionError, match="position 1 of 1"):
        tape.assert_order(record)


def test_assert_order_accepts_filtered_logs_as_steps() -> None:
    charge = binding(Gateway, "charge")
    record = binding(Ledger, "record")

    with timeline(charge, record) as tape:
        gateway = Gateway()
        gateway.charge(500)
        gateway.charge(1)
        gateway.charge(500)
        Ledger().record({"status": "failed"})

        # Inside the block, logs come from binding.events; each log
        # holds every matching event, and repeating one means "another
        # such event, later".

        tape.assert_order(
            charge.events.with_args(amount=500),
            charge.events.with_args(amount=500),
            record.events.with_args(entry={"status": "failed"}),
        )

        # Bindings and logs mix.

        tape.assert_order(charge.events.with_args(amount=1), charge, record)

    # After the block, the same through the tape, and outcome filters
    # serve as literals too.

    charge_500 = tape.for_binding(charge).with_args(amount=500)
    assert tape.assert_order(charge_500, charge_500, tape.for_binding(record)) is tape
    returned_one = tape.for_binding(charge).returning({"id": "ch_1", "amount": 1})
    tape.assert_order(returned_one, record)


def test_assert_order_log_step_stalls_when_not_enough_match() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge) as tape:
        Gateway().charge(500)
        Gateway().charge(1)

    charge_500 = tape.for_binding(charge).with_args(amount=500)

    with pytest.raises(AssertionError) as failure:
        tape.assert_order(charge_500, charge_500)

    assert (
        "stalled waiting for test_timeline:Gateway.charge[amount=500] (position 2 of 2)"
    ) in str(failure.value)


def test_assert_order_log_from_another_tape_never_matches() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge) as first:
        Gateway().charge(500)

    with timeline(charge) as second:
        Gateway().charge(500)

    with pytest.raises(AssertionError, match="position 1 of 1"):
        second.assert_order(first.for_binding(charge))


def test_assert_order_rejects_other_steps() -> None:
    with timeline() as tape:
        pass

    with pytest.raises(TypeError, match="bindings or event logs"):
        tape.assert_order("charge")


def test_assert_order_consecutive_ignores_unnamed_bindings() -> None:
    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")
    record = binding(Ledger, "record")

    with timeline(process, charge, record) as tape:
        Processor().process("widget")

    # process wraps the other two, so charge and record are consecutive
    # among the named bindings' events even though process sits around
    # them in the tape.

    tape.assert_order(charge, record, consecutive=True)
    tape.assert_order(charge, record, exact=True)

    # Naming process as well makes its events count again.

    tape.assert_order(process, charge, record, consecutive=True)


def test_assert_order_consecutive_fails_on_a_named_event_between() -> None:
    charge = binding(Gateway, "charge")
    record = binding(Ledger, "record")

    with timeline(charge, record) as tape:
        gateway = Gateway()
        gateway.charge(500)
        gateway.charge(1)
        Ledger().record({"status": "ok"})

    charge_500 = tape.for_binding(charge).with_args(amount=500)

    tape.assert_order(charge_500, record)

    with pytest.raises(AssertionError) as failure:
        tape.assert_order(charge_500, record, consecutive=True)

    message = str(failure.value)
    assert "expected consecutive events" in message
    assert "after test_timeline:Gateway.charge[amount=500] (position 1 of 2)" in message
    assert (
        "saw test_timeline:Gateway.charge(amount=1) where"
        " test_timeline:Ledger.record" in message
    )
    assert "(position 2 of 2) was expected" in message
    assert "actual timeline:" in message

    # Events before the first step and after the last are free.

    charge_1 = tape.for_binding(charge).with_args(amount=1)
    tape.assert_order(charge_1, record, consecutive=True)
    tape.assert_order(charge, consecutive=True)


def test_assert_order_exact_requires_nothing_before_or_after() -> None:
    charge = binding(Gateway, "charge")
    record = binding(Ledger, "record")

    with timeline(charge, record) as tape:
        gateway = Gateway()
        gateway.charge(500)
        gateway.charge(500)
        Ledger().record({"status": "failed"})

    charge_500 = tape.for_binding(charge).with_args(amount=500)
    failed = tape.for_binding(record).with_args(entry={"status": "failed"})

    tape.assert_order(charge_500, charge_500, failed, exact=True)
    tape.assert_order(charge, charge, record, exact=True)

    with pytest.raises(AssertionError) as before:
        tape.assert_order(charge_500, failed, exact=True)
    assert "expected consecutive events" in str(before.value)

    # The record event is invisible when no step names its binding, so
    # this is exact for charge alone.

    tape.assert_order(charge_500, charge_500, exact=True)

    # A named binding's event before the first step fails exact.

    second = tape.all[1]

    def second_charge(event: Event) -> bool:
        return event is second

    with pytest.raises(AssertionError) as leading:
        tape.assert_order(
            tape.for_binding(charge).matching(second_charge), record, exact=True
        )
    assert "saw test_timeline:Gateway.charge(amount=500) before" in str(leading.value)
    assert (
        "test_timeline:Gateway.charge[matching=second_charge] (position 1 of 2)"
    ) in str(leading.value)

    # A filtered log makes the binding's other events visible.

    with timeline(charge) as tape:
        Gateway().charge(500)
        Gateway().charge(1)

    with pytest.raises(
        AssertionError,
        match="saw test_timeline:Gateway.charge\\(amount=1\\) after",
    ):
        tape.assert_order(tape.for_binding(charge).with_args(amount=500), exact=True)


def test_assert_order_exact_implies_consecutive() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge) as tape:
        Gateway().charge(500)
        Gateway().charge(1)
        Gateway().charge(500)

    charge_500 = tape.for_binding(charge).with_args(amount=500)

    with pytest.raises(AssertionError, match="expected consecutive events"):
        tape.assert_order(charge_500, charge_500, consecutive=False, exact=True)


# ---------------------------------------------------------------------------
# declared expectations
# ---------------------------------------------------------------------------


def test_a_met_expectation_passes_silently() -> None:
    charge = binding(Gateway, "charge").expect_once()

    with timeline(charge):
        Gateway().charge(500)


def test_an_unmet_expectation_raises_at_timeline_exit() -> None:
    charge = binding(Gateway, "charge").expect_times(2)

    with pytest.raises(ExpectationNotMetError) as failure:
        with timeline(charge):
            Gateway().charge(500)

    message = str(failure.value)
    assert "Gateway.charge" in message
    assert "expected exactly 2 event(s), got 1" in message


def test_expect_never_catches_an_unexpected_call() -> None:
    refund = binding(Gateway, "refund").expect_never()

    with pytest.raises(ExpectationNotMetError):
        with timeline(refund):
            Gateway().refund(100)


def test_expect_at_least() -> None:
    charge = binding(Gateway, "charge").expect_at_least(2)

    with timeline(charge):
        Gateway().charge(100)
        Gateway().charge(200)

    with pytest.raises(ExpectationNotMetError):
        with timeline(charge):
            Gateway().charge(300)


def test_multiple_expectations_are_all_verified() -> None:
    charge = binding(Gateway, "charge").expect_at_least(1)
    refund = binding(Gateway, "refund").expect_never()

    with timeline(charge, refund):
        Gateway().charge(500)


def test_group_member_expectations_are_verified() -> None:
    group = bindings(
        charge=binding(Gateway, "charge"), refund=binding(Gateway, "refund")
    )
    group.refund.expect_never()

    with pytest.raises(ExpectationNotMetError, match="refund"):
        with timeline(group):
            Gateway().refund(100)


def test_verification_is_skipped_when_the_block_already_raised() -> None:
    charge = binding(Gateway, "charge").expect_once()

    # The expectation is unmet, but the in-flight failure is the real
    # cause and must not be buried by verification.

    with pytest.raises(ValueError, match="real cause"):
        with timeline(charge):
            raise ValueError("real cause")


def test_expectations_persist_across_timelines_like_behaviour() -> None:
    charge = binding(Gateway, "charge").expect_once()

    with timeline(charge):
        Gateway().charge(500)

    with pytest.raises(ExpectationNotMetError):
        with timeline(charge):
            pass


def test_expectation_declarations_chain_with_behaviour() -> None:
    charge = binding(Gateway, "charge").expect_once()
    charge.on_call.returns({"id": "stub"})

    with timeline(charge):
        assert Gateway().charge(500) == {"id": "stub"}


# ---------------------------------------------------------------------------
# task isolation
# ---------------------------------------------------------------------------


def test_concurrent_tasks_record_isolated_trees() -> None:
    async def work(tape: Tape, name: str) -> Event:
        root = _record_event(Event("call", f"{name}.root"), (tape,))
        root_token = _push(root)
        await asyncio.sleep(0)

        child = _record_event(Event("call", f"{name}.child"), (tape,))
        child_token = _push(child)
        await asyncio.sleep(0)

        _pop(child_token)
        _pop(root_token)
        return root

    async def main() -> tuple[Tape, list[Event]]:
        with timeline() as tape:
            roots = await asyncio.gather(*(work(tape, n) for n in ("a", "b", "c")))
        return tape, list(roots)

    tape, roots = asyncio.run(main())

    # Each task sees only its own stack: every root records at depth 0
    # with its own child nested beneath it, however the tasks interleave.

    for root in roots:
        assert root.depth == 0
        assert root.parent_id is None
        assert [child.parent_id for child in tape.children_of(root)] == [root.seq]

    # Sequence numbers are process-wide, so the absolute values depend
    # on what recorded before this test; the six events still allocate
    # a contiguous, strictly ordered block.

    seqs = sorted(event.seq for event in tape.all)
    assert seqs == list(range(seqs[0], seqs[0] + 6))
