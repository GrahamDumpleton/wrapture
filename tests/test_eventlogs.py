"""Tests for the EventLog: access guards, filters, and data access.

Filters follow one rule: they narrow and return, never raise. The
integration tests drive real bindings inside a timeline; the filters
that bridge attribute events (returning on gets, with_value on sets)
are tested against hand-built events until attribute recording lands.
"""

from typing import Any

import pytest

from wrapture import Event, EventLog, NeverAppliedError, binding, timeline


class Gateway:
    def charge(self, amount: int, currency: str = "USD") -> dict[str, Any]:
        return {"id": f"ch_{amount}", "amount": amount}

    def refund(self, amount: int) -> dict[str, Any]:
        raise TimeoutError("gateway down")


# ---------------------------------------------------------------------------
# access guards: loud errors where an empty log would lie
# ---------------------------------------------------------------------------


def test_events_raises_if_the_binding_was_never_applied() -> None:
    charge = binding(Gateway, "charge")

    with timeline():
        with pytest.raises(NeverAppliedError):
            _ = charge.events


def test_events_raises_outside_a_timeline() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        pass

    with pytest.raises(RuntimeError, match="inside a timeline"):
        _ = charge.events


def test_events_returns_only_this_bindings_events() -> None:
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")

    with timeline(charge, refund) as tape:
        Gateway().charge(500)

        assert charge.events.count == 1
        assert refund.events.count == 0
        assert charge.events.first is tape.all[0]


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


def test_of_kind_narrows_by_event_kind() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        assert charge.events.of_kind("call").count == 1
        assert charge.events.of_kind("get", "set").count == 0


def test_matching_narrows_by_predicate() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(100)
        Gateway().charge(300)

        big = charge.events.matching(
            lambda e: e.arguments is not None and e.arguments["amount"] > 200
        )
        assert big.count == 1
        assert big.first.args == (300,)


def test_raising_narrows_by_exception_type() -> None:
    refund = binding(Gateway, "refund")

    with timeline(refund):
        with pytest.raises(TimeoutError):
            Gateway().refund(100)

        assert refund.events.raising(TimeoutError).count == 1
        assert refund.events.raising(ValueError).count == 0
        assert refund.events.raising().count == 1


def test_with_args_matches_normalized_arguments() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        events = charge.events
        assert events.with_args(amount=500).count == 1
        assert events.with_args(currency="USD").count == 1
        assert events.with_args(amount=500, currency="USD").count == 1
        assert events.with_args(amount=999).count == 0
        assert events.with_args(amount=500, currency="AUD").count == 0
        assert events.with_args(unknown=1).count == 0


def test_returning_matches_the_recorded_result() -> None:
    charge = binding(Gateway, "charge").on_call.returns({"id": "stub"})
    refund = binding(Gateway, "refund")

    with timeline(charge, refund):
        Gateway().charge(500)
        with pytest.raises(TimeoutError):
            Gateway().refund(100)

        assert charge.events.returning({"id": "stub"}).count == 1
        assert charge.events.returning({"id": "other"}).count == 0

        # A call that raised has no outcome, so it never matches.

        assert refund.events.returning(None).count == 0


def test_filters_compose_and_chain_the_label() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)
        Gateway().charge(700)

        narrowed = charge.events.with_args(amount=700).raising()
        assert narrowed.count == 0
        assert narrowed.label == "Gateway.charge[amount=700][raising]"


# ---------------------------------------------------------------------------
# filters that bridge attribute events, against hand-built logs
# ---------------------------------------------------------------------------


def test_returning_bridges_call_results_and_read_values() -> None:
    log = EventLog(
        "Model.author",
        [
            Event("get", "m:Model.author", result="graham"),
            Event("get", "m:Model.author", result="other"),
            Event("call", "m:Model.load", result="graham"),
        ],
    )

    assert log.returning("graham").count == 2


def test_with_value_matches_the_value_written() -> None:
    log = EventLog(
        "Model.author",
        [
            Event("set", "m:Model.author", value="graham"),
            Event("set", "m:Model.author", value="other"),
            Event("get", "m:Model.author", result="graham"),
        ],
    )

    assert log.with_value("graham").count == 1
    assert log.with_value("graham").first.kind == "set"


def test_with_args_on_attribute_events_narrows_to_empty() -> None:
    # Filters are not gated on mode: a mismatched filter yields an empty
    # log rather than raising, so it stays safe on mixed logs.

    log = EventLog("Model.author", [Event("get", "m:Model.author")])

    assert log.with_args(amount=500).count == 0


# ---------------------------------------------------------------------------
# data access
# ---------------------------------------------------------------------------


def test_data_accessors() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(100)
        Gateway().charge(200)

        events = charge.events

        assert events.count == 2
        assert len(events) == 2
        assert bool(events)
        assert events.first.args == (100,)
        assert events.last.args == (200,)
        assert events[1] is events.last
        assert [e.args for e in events] == [(100,), (200,)]

        empty = events.with_args(amount=999)
        assert not empty
        assert empty.count == 0


def test_a_bare_assert_reads_naturally() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        assert charge.events.with_args(amount=500)
        assert not charge.events.raising()


def test_repr_prints_the_events() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        shown = repr(charge.events)
        assert "<EventLog Gateway.charge: 1 event(s)>" in shown
        assert "Gateway.charge(amount=500, currency='USD')" in shown

        empty = repr(charge.events.raising())
        assert "0 event(s)" in empty
        assert "(no events)" in empty
