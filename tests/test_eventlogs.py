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


class Dispatcher:
    def submit(
        self,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        route_name: str | None = None,
        **options: Any,
    ) -> None:
        pass


class Sink:
    def write(self, **entries: Any) -> None:
        pass


class Basket(dict[str, int]):
    # dict subclass: two baskets with the same content compare equal,
    # so identity and equality filtering give different answers.

    def total(self) -> int:
        return sum(self.values())


class Registry:
    @classmethod
    def register(cls, name: str) -> str:
        return name


def helper(amount: int) -> int:
    return amount


def test_with_instance_narrows_by_identity_not_equality() -> None:
    total = binding(Basket, "total")

    first = Basket(apple=1)
    second = Basket(apple=1)
    assert first == second

    with timeline(total):
        first.total()
        first.total()
        second.total()

        total.events.with_instance(first).assert_times(2)
        total.events.with_instance(second).assert_once()
        total.events.with_instance(Basket(apple=1)).assert_never()


def test_with_instance_matches_the_class_for_classmethods() -> None:
    register = binding(Registry, "register")

    with timeline(register):
        Registry.register("a")

        register.events.with_instance(Registry).assert_once()


def test_with_instance_chains_with_other_filters() -> None:
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")

    paying = Gateway()
    other = Gateway()

    with timeline(charge, refund):
        paying.charge(500)
        other.charge(500)
        with pytest.raises(TimeoutError):
            paying.refund(100)

        charge.events.with_instance(paying).with_args(amount=500).assert_once()
        refund.events.with_instance(paying).raising(TimeoutError).assert_once()
        refund.events.with_instance(other).assert_never()


def test_with_instance_never_matches_instance_less_events() -> None:
    import sys

    module = sys.modules[__name__]
    bound = binding(module, "helper")

    with timeline(bound):
        module.helper(5)

        bound.events.assert_once()
        bound.events.with_instance(module).assert_never()
        bound.events.with_instance(None).assert_never()


def test_with_instance_shows_in_the_filter_label() -> None:
    total = binding(Basket, "total")

    basket = Basket(apple=1)
    with timeline(total):
        basket.total()

        narrowed = total.events.with_instance(basket)
        assert "[instance=" in narrowed.label


def test_with_args_falls_through_into_the_var_keyword_bundle() -> None:
    submit = binding(Dispatcher, "submit")

    with timeline(submit):
        Dispatcher().submit((4,), parent_id="id-1", root_id="root", priority=None)

        events = submit.events

        # Top-level partial match as always: unnamed parameters are free.
        assert events.with_args(args=(4,)).count == 1

        # Names that are not parameters resolve inside the bundle, other
        # bundle keys free.
        assert events.with_args(parent_id="id-1").count == 1
        assert events.with_args(parent_id="id-1", root_id="root").count == 1
        assert events.with_args(parent_id="other").count == 0
        assert events.with_args(chain=[]).count == 0

        # Parameters and bundle keys mix in one call.
        assert events.with_args(args=(4,), parent_id="id-1", priority=None).count == 1

        # The ordinary parameter that happens to be called kwargs is
        # just a parameter.
        assert events.with_args(kwargs=None).count == 1


def test_with_args_on_the_bundle_parameter_itself_is_exact() -> None:
    submit = binding(Dispatcher, "submit")

    with timeline(submit):
        Dispatcher().submit((4,), parent_id="id-1", root_id="root")

        events = submit.events
        assert (
            events.with_args(options={"parent_id": "id-1", "root_id": "root"}).count
            == 1
        )
        assert events.with_args(options={"parent_id": "id-1"}).count == 0


def test_with_args_without_a_var_keyword_narrows_to_empty() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        assert charge.events.with_args(parent_id="id-1").count == 0


def test_with_args_name_colliding_with_the_bundle_compares_whole() -> None:
    # A call passing a keyword named like the bundle parameter records
    # {"entries": {"entries": 1}}; the name resolves as the parameter,
    # compared whole, and the exact form still reaches the key.

    write = binding(Sink, "write")

    with timeline(write):
        Sink().write(entries=1)

        assert write.events.with_args(entries=1).count == 0
        assert write.events.with_args(entries={"entries": 1}).count == 1


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
        assert narrowed.label == "test_eventlogs:Gateway.charge[amount=700][raising]"


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
# assertions: raise on failure, return self so they chain
# ---------------------------------------------------------------------------


def test_assertions_pass_and_chain() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)
        Gateway().charge(500)

        events = charge.events

        assert events.assert_times(2) is events
        events.assert_any().assert_at_least(1).assert_at_most(2)
        events.with_args(amount=500).assert_times(2)
        events.raising().assert_never()

        first = events.with_args(amount=500).assert_any().first
        assert first.args == (500,)


def test_assert_once_and_times_fail_with_the_events_shown() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        with pytest.raises(AssertionError) as failure:
            charge.events.assert_times(3)

        message = str(failure.value)
        assert "expected exactly 3 event(s), got 1" in message
        assert "<EventLog test_eventlogs:Gateway.charge: 1 event(s)>" in message
        assert "test_eventlogs:Gateway.charge(amount=500, currency='USD')" in message


def test_assert_never_failure_shows_the_offending_events() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        with pytest.raises(AssertionError) as failure:
            charge.events.assert_never()

        assert "expected no events, got 1" in str(failure.value)


def test_bound_assertions_fail_on_the_boundary() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        with pytest.raises(AssertionError, match="at least 2"):
            charge.events.assert_at_least(2)

        with pytest.raises(AssertionError, match="at most 0"):
            charge.events.assert_at_most(0)

        with pytest.raises(AssertionError, match="at least 1"):
            charge.events.raising().assert_any()


def test_an_over_narrowed_log_falls_back_to_what_was_discarded() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        with pytest.raises(AssertionError) as failure:
            charge.events.with_args(amount=999).assert_once()

        # The empty narrowed log alone would be mysterious; the message
        # shows the nearest non-empty log in the filter chain, so the
        # discarded event is visible.

        message = str(failure.value)
        assert "expected exactly 1 event(s), got 0" in message
        assert (
            "<EventLog test_eventlogs:Gateway.charge[amount=999]: 0 event(s)>"
            in message
        )
        assert "(no events)" in message
        assert "filtered from:" in message
        assert "test_eventlogs:Gateway.charge(amount=500, currency='USD')" in message


def test_fallback_walks_past_empty_intermediate_logs() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        with pytest.raises(AssertionError) as failure:
            charge.events.with_args(amount=999).raising().assert_once()

        message = str(failure.value)
        assert "filtered from:" in message
        assert "<EventLog test_eventlogs:Gateway.charge: 1 event(s)>" in message


def test_no_fallback_when_nothing_was_ever_recorded() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        with pytest.raises(AssertionError) as failure:
            charge.events.raising().assert_any()

        assert "filtered from:" not in str(failure.value)


def test_a_mistyped_assertion_is_an_attribute_error() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        with pytest.raises(AttributeError):
            charge.events.assert_calld_once()  # type: ignore[attr-defined]


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
        assert "<EventLog test_eventlogs:Gateway.charge: 1 event(s)>" in shown
        assert "test_eventlogs:Gateway.charge(amount=500, currency='USD')" in shown

        empty = repr(charge.events.raising())
        assert "0 event(s)" in empty
        assert "(no events)" in empty
