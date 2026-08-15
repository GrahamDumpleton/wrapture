"""Tests that calls through a binding record events onto the tape.

These cover the callable-mode recording path: what an event contains,
how calls nest, how exceptions and behaviour show up, and the ways a
call legitimately records nothing (no timeline, suspension, and the
recorder's own reentrancy guard).
"""

import asyncio
from typing import Any

import pytest
from wrapt import MISSING

from wrapture import binding, bindings, timeline
from wrapture.timeline import _in_recorder


class Ledger:
    def record(self, entry: dict[str, Any]) -> str:
        return f"ledger:{entry['id']}"


class Gateway:
    def __init__(self) -> None:
        self.ledger = Ledger()

    def charge(self, amount: int, currency: str = "USD") -> dict[str, Any]:
        entry = {"id": f"ch_{amount}", "amount": amount}
        self.ledger.record(entry)
        return entry

    def refund(self, amount: int) -> dict[str, Any]:
        raise TimeoutError("gateway down")


# ---------------------------------------------------------------------------
# what one recorded call contains
# ---------------------------------------------------------------------------


def test_a_call_records_one_event_with_its_details() -> None:
    charge = binding(Gateway, "charge")
    gateway = Gateway()

    with timeline(charge) as tape:
        gateway.charge(500)

    (event,) = tape.all

    assert event.kind == "call"
    assert event.path == f"{Gateway.__module__}:Gateway.charge"
    assert event.label == "Gateway.charge"
    assert event.binding is charge
    assert event.instance is gateway
    assert event.seq == 1
    assert event.depth == 0
    assert event.args == (500,)
    assert event.arguments == {"amount": 500, "currency": "USD"}
    assert event.result == {"id": "ch_500", "amount": 500}
    assert event.exception is None
    assert event.forwarded is None


def test_call_forms_record_the_same_normalized_arguments() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge) as tape:
        Gateway().charge(500, "USD")
        Gateway().charge(500, currency="USD")

    first, second = tape.all
    assert first.arguments == second.arguments


def test_a_label_override_never_touches_the_path() -> None:
    charge = binding(Gateway, "charge", label="stubbed charge")

    with timeline(charge) as tape:
        Gateway().charge(500)

    (event,) = tape.all

    assert event.path == f"{Gateway.__module__}:Gateway.charge"
    assert event.label == "stubbed charge"


# ---------------------------------------------------------------------------
# nesting
# ---------------------------------------------------------------------------


def test_nested_calls_record_a_tree() -> None:
    charge = binding(Gateway, "charge")
    record = binding(Ledger, "record")

    with timeline(charge, record) as tape:
        Gateway().charge(500)

    outer, inner = tape.all

    assert outer.label == "Gateway.charge"
    assert inner.label == "Ledger.record"
    assert inner.parent is outer
    assert inner.depth == 1
    assert outer.children == [inner]
    assert (outer.seq, inner.seq) == (1, 2)


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------


def test_a_raising_call_records_the_exception_and_propagates_it() -> None:
    refund = binding(Gateway, "refund")

    with timeline(refund) as tape:
        with pytest.raises(TimeoutError):
            Gateway().refund(100)

    (event,) = tape.all

    assert isinstance(event.exception, TimeoutError)
    assert event.result is MISSING


def test_an_injected_failure_is_recorded_as_the_exception() -> None:
    charge = binding(Gateway, "charge").on_call.raises(TimeoutError("injected"))

    with timeline(charge) as tape:
        with pytest.raises(TimeoutError):
            Gateway().charge(500)

    (event,) = tape.all
    assert isinstance(event.exception, TimeoutError)


# ---------------------------------------------------------------------------
# behaviour and recording together
# ---------------------------------------------------------------------------


def test_a_stubbed_call_records_the_injected_result() -> None:
    charge = binding(Gateway, "charge").on_call.returns({"id": "stub"})

    with timeline(charge) as tape:
        Gateway().charge(500)

    (event,) = tape.all

    assert event.result == {"id": "stub"}
    assert event.arguments == {"amount": 500, "currency": "USD"}


def test_transformed_arguments_record_what_was_forwarded() -> None:
    def halve(
        args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        return (args[0] // 2,), kwargs

    charge = binding(Gateway, "charge").on_call.transforms_args(halve)

    with timeline(charge) as tape:
        result = Gateway().charge(500)

    event = tape.all[0]

    # The event keeps both sides: what the caller sent, and what the
    # wrapped function actually received after the transform.

    assert result["amount"] == 250
    assert event.args == (500,)
    assert event.forwarded == ((250,), {})


def test_a_transformed_result_records_what_flowed_downstream() -> None:
    charge = binding(Gateway, "charge").on_call.transforms_result(
        lambda result: {**result, "traced": True}
    )

    with timeline(charge) as tape:
        result = Gateway().charge(500)

    assert result["traced"] is True
    assert tape.all[0].result is result


# ---------------------------------------------------------------------------
# ways a call records nothing
# ---------------------------------------------------------------------------


def test_no_timeline_means_no_recording_but_behaviour_still_applies() -> None:
    charge = binding(Gateway, "charge").on_call.returns({"id": "stub"})

    with charge:
        assert Gateway().charge(500) == {"id": "stub"}


def test_a_suspended_binding_records_nothing_but_counts_the_calls() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge) as tape:
        charge.suspend()
        Gateway().charge(500)
        charge.resume()
        Gateway().charge(600)

    assert [e.args for e in tape.all] == [(600,)]
    assert charge.suspended_calls == 1


def test_the_recorder_guard_skips_recording_but_not_behaviour() -> None:
    charge = binding(Gateway, "charge").on_call.returns({"id": "stub"})

    with timeline(charge) as tape:
        guard = _in_recorder.set(True)
        try:
            assert Gateway().charge(500) == {"id": "stub"}
        finally:
            _in_recorder.reset(guard)

        Gateway().charge(600)

    assert [e.args for e in tape.all] == [(600,)]


# ---------------------------------------------------------------------------
# groups record too
# ---------------------------------------------------------------------------


def test_group_members_record_with_their_group_names() -> None:
    group = bindings(charge=(Gateway, "charge"), record=(Ledger, "record"))

    with timeline(group) as tape:
        Gateway().charge(500)

    # The group's keyword names become the labels; the paths keep the
    # real locations regardless.

    assert [e.label for e in tape.all] == ["charge", "record"]
    assert [e.path for e in tape.all] == [
        f"{Gateway.__module__}:Gateway.charge",
        f"{Ledger.__module__}:Ledger.record",
    ]
    assert [e.binding for e in tape.all] == [group.charge, group.record]


# ---------------------------------------------------------------------------
# async targets
# ---------------------------------------------------------------------------


class AsyncGateway:
    def __init__(self) -> None:
        self.ledger = Ledger()

    async def charge(self, amount: int) -> dict[str, Any]:
        await asyncio.sleep(0)
        entry = {"id": f"ch_{amount}", "amount": amount}
        self.ledger.record(entry)
        return entry

    async def refund(self, amount: int) -> None:
        raise TimeoutError("gateway down")


def test_an_async_call_records_the_awaited_result() -> None:
    charge = binding(AsyncGateway, "charge")

    with timeline(charge) as tape:
        result = asyncio.run(AsyncGateway().charge(500))

    (event,) = tape.all

    assert result == {"id": "ch_500", "amount": 500}
    assert event.result is result
    assert event.arguments == {"amount": 500}


def test_an_async_failure_records_the_exception() -> None:
    refund = binding(AsyncGateway, "refund")

    with timeline(refund) as tape:
        with pytest.raises(TimeoutError):
            asyncio.run(AsyncGateway().refund(100))

    (event,) = tape.all
    assert isinstance(event.exception, TimeoutError)
    assert event.result is MISSING


def test_calls_inside_a_coroutine_body_nest_under_its_event() -> None:
    charge = binding(AsyncGateway, "charge")
    record = binding(Ledger, "record")

    with timeline(charge, record) as tape:
        asyncio.run(AsyncGateway().charge(500))

    outer, inner = tape.all

    assert outer.label == "AsyncGateway.charge"
    assert inner.label == "Ledger.record"
    assert inner.parent is outer
    assert outer.children == [inner]
