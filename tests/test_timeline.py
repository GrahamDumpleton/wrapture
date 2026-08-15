"""Tests for the tape, the ambient recording state, and timeline scoping.

The wrappers do not emit events yet, so these tests drive the tape and
the nesting helpers by hand: that is the contract this layer provides to
the recording wiring built on top of it.
"""

import asyncio
from typing import Any

import pytest

from wrapture import AlreadyAppliedError, Event, Tape, binding, bindings, timeline
from wrapture.timeline import _pop, _push, _stack, _tape


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


def test_tape_assigns_sequence_numbers_in_record_order() -> None:
    tape = Tape()
    first = tape.record(Event("call", "a"))
    second = tape.record(Event("call", "b"))

    assert (first.seq, second.seq) == (1, 2)
    assert tape.all == [first, second]


def test_tape_all_returns_a_copy() -> None:
    tape = Tape()
    event = tape.record(Event("call", "a"))

    tape.all.clear()
    assert tape.all == [event]


# ---------------------------------------------------------------------------
# nesting: push and pop
# ---------------------------------------------------------------------------


def test_push_assigns_parent_depth_and_children() -> None:
    outer = Event("call", "outer")
    inner = Event("call", "inner")

    outer_token = _push(outer)
    assert outer.depth == 0
    assert outer.parent is None

    inner_token = _push(inner)
    assert inner.depth == 1
    assert inner.parent is outer
    assert outer.children == [inner]

    # Popping the inner event makes the next push a sibling, not a
    # grandchild.

    _pop(inner_token)
    sibling = Event("call", "sibling")
    sibling_token = _push(sibling)

    assert sibling.depth == 1
    assert sibling.parent is outer
    assert outer.children == [inner, sibling]

    _pop(sibling_token)
    _pop(outer_token)
    assert _stack.get() == ()


# ---------------------------------------------------------------------------
# the timeline scope
# ---------------------------------------------------------------------------


def test_no_ambient_tape_outside_a_timeline() -> None:
    assert _tape.get() is None

    with timeline() as tape:
        assert isinstance(tape, Tape)
        assert _tape.get() is tape

    assert _tape.get() is None


def test_timeline_applies_bindings_on_entry_and_removes_on_exit() -> None:
    charge = binding(Gateway, "charge").on_call.returns({"id": "stub"})

    with timeline(charge):
        assert charge.active
        assert Gateway().charge(1) == {"id": "stub"}

    assert not charge.active
    assert Gateway().charge(1) == {"id": "ch_1", "amount": 1}


def test_timeline_accepts_groups_and_iterables() -> None:
    group = bindings(charge=(Gateway, "charge"), record=(Ledger, "record"))
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
        assert _tape.get() is None
        assert _stack.get() == ()
    finally:
        second.remove()


def test_timeline_reuse_accumulates_on_the_same_tape() -> None:
    scope = timeline()

    with scope as tape:
        tape.record(Event("call", "a"))

    with scope as again:
        assert again is tape
        again.record(Event("call", "b"))

    assert [event.path for event in tape.all] == ["a", "b"]
    assert [event.seq for event in tape.all] == [1, 2]


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
        # restores both the outer tape and the outer stack.

        with timeline() as inner:
            assert _tape.get() is inner
            assert _stack.get() == ()

        assert _tape.get() is outer
        assert _stack.get() == (in_progress,)
        _pop(token)


# ---------------------------------------------------------------------------
# task isolation
# ---------------------------------------------------------------------------


def test_concurrent_tasks_record_isolated_trees() -> None:
    async def work(tape: Tape, name: str) -> Event:
        root = tape.record(Event("call", f"{name}.root"))
        root_token = _push(root)
        await asyncio.sleep(0)

        child = tape.record(Event("call", f"{name}.child"))
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
        assert root.parent is None
        assert [child.parent for child in root.children] == [root]

    assert sorted(event.seq for event in tape.all) == [1, 2, 3, 4, 5, 6]
