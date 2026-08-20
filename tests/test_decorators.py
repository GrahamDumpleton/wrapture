"""Tests for the decorator forms: bound() and taped()."""

import asyncio
import inspect
import os
import unittest
from typing import Any

import pytest

from wrapture import Binding, WrongModeError, binding, bound, taped, timeline


class Gateway:
    def charge(self, amount: int, currency: str = "USD") -> dict[str, Any]:
        return {"id": f"ch_{amount}", "amount": amount}


class Model:
    status = 3


class PushClient:
    async def send(self, user: str) -> str:
        raise RuntimeError("no gateway in tests")


# ---------------------------------------------------------------------------
# injection and lifecycle
# ---------------------------------------------------------------------------


def test_bound_applies_around_the_call_and_injects_the_binding() -> None:
    @taped()
    @bound(Gateway, "charge").on_call.returns({"id": "STUB"})
    def body(tape: Any, charge: Any) -> str:
        assert isinstance(charge, Binding)
        assert Gateway().charge(5) == {"id": "STUB"}

        charge.events.assert_once()
        tape.assert_order(charge)

        return "done"

    assert body() == "done"
    assert Gateway().charge(2) == {"id": "ch_2", "amount": 2}


def test_a_bare_bound_is_observe_only() -> None:
    @taped()
    @bound(Gateway, "charge")
    def body(tape: Any, charge: Any) -> None:
        assert Gateway().charge(3) == {"id": "ch_3", "amount": 3}
        charge.events.with_args(amount=3).assert_once()

    body()


def test_each_call_gets_a_fresh_binding() -> None:
    @taped()
    @bound(Gateway, "charge").on_call.returns({"id": "STUB"})
    def body(tape: Any, charge: Any) -> None:
        Gateway().charge(1)
        charge.events.assert_once()

    body()
    body()


def test_the_return_value_passes_through() -> None:
    @bound(Gateway, "charge").on_call.returns({"id": "STUB"})
    def body(charge: Any) -> int:
        return 41

    assert body() == 41


def test_injection_lands_in_var_keyword_when_no_parameter_matches() -> None:
    @bound(Gateway, "charge").on_call.returns({"id": "STUB"})
    def body(**kwargs: Any) -> None:
        assert isinstance(kwargs["charge"], Binding)
        assert Gateway().charge(1) == {"id": "STUB"}

    body()


def test_phases_configure_in_the_body_through_the_handle() -> None:
    @taped()
    @bound(Gateway, "charge")
    def body(tape: Any, charge: Any) -> None:
        flaky = charge.on_call.then(after=1)
        flaky.raises(TimeoutError("busy"))
        flaky.then(after=1).returns({"id": "fallback"})

        gw = Gateway()
        assert gw.charge(1) == {"id": "ch_1", "amount": 1}
        with pytest.raises(TimeoutError):
            gw.charge(2)
        assert gw.charge(3) == {"id": "fallback"}

    body()


def test_body_created_bindings_record_onto_the_decorators_tape() -> None:
    class Ledger:
        def record(self, entry: str) -> None:
            pass

    @taped()
    @bound(Gateway, "charge").on_call.returns({"id": "STUB"})
    def body(tape: Any, charge: Any) -> None:
        record = binding(Ledger, "record")

        with record:
            Gateway().charge(1)
            Ledger().record("entry")

            tape.assert_order(charge, record)

    body()


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


def test_stages_and_terminal_chain_on_one_channel() -> None:
    @(
        bound(Gateway, "charge")
        .on_call.transforms_result(lambda r: {**r, "seen": True})
        .returns({"id": "STUB"})
    )
    def body(charge: Any) -> None:
        assert Gateway().charge(1) == {"id": "STUB", "seen": True}

    body()


def test_a_repeated_terminal_in_the_chain_is_last_wins() -> None:
    @bound(Gateway, "charge").on_call.returns({"id": "first"}).returns({"id": "second"})
    def body(charge: Any) -> None:
        assert Gateway().charge(1) == {"id": "second"}

    body()


def test_an_attribute_binding_configures_its_own_channel() -> None:
    @bound(Model, "status").on_get.returns(5)
    def body(status: Any) -> None:
        assert Model().status == 5

    body()

    assert Model().status == 3


def test_then_and_advance_error_at_decoration() -> None:
    with pytest.raises(TypeError, match=r"then\(\) is not available"):
        bound(Gateway, "charge").on_call.then(after=1)

    with pytest.raises(TypeError, match=r"advance\(\) is not available"):
        bound(Gateway, "charge").on_call.advance()


def test_an_unknown_verb_errors_at_decoration() -> None:
    with pytest.raises(AttributeError, match="no verb 'rejects'"):
        bound(Gateway, "charge").on_call.rejects()


def test_channel_use_on_a_value_binding_errors_at_decoration() -> None:
    with pytest.raises(WrongModeError, match="on_call is not available"):
        _ = bound(os.environ, item="API_KEY").on_call


def test_value_verbs_on_a_positional_binding_error_at_decoration() -> None:
    with pytest.raises(WrongModeError, match=r"overrides\(\) is only available"):
        bound(Gateway, "charge").overrides(1)


def test_value_verb_subsets_match_the_modes() -> None:
    with pytest.raises(WrongModeError, match=r"updates\(\)"):
        bound(os.environ, item="API_KEY").updates({"a": 1})

    with pytest.raises(WrongModeError, match=r"hides\(\)"):
        bound(Model, "status", mode="mapping").hides()


# ---------------------------------------------------------------------------
# value and mapping bindings
# ---------------------------------------------------------------------------


def test_a_value_binding_pins_and_restores_an_environment_variable() -> None:
    assert "WRAPTURE_TEST_KEY" not in os.environ

    @bound(os.environ, item="WRAPTURE_TEST_KEY").overrides("sk_test")
    def body(WRAPTURE_TEST_KEY: Any) -> None:
        assert os.environ["WRAPTURE_TEST_KEY"] == "sk_test"

    body()
    assert "WRAPTURE_TEST_KEY" not in os.environ


def test_hides_keeps_the_slot_absent() -> None:
    os.environ["WRAPTURE_TEST_KEY"] = "present"
    try:

        @bound(os.environ, item="WRAPTURE_TEST_KEY").hides()
        def body(WRAPTURE_TEST_KEY: Any) -> None:
            assert "WRAPTURE_TEST_KEY" not in os.environ

        body()
        assert os.environ["WRAPTURE_TEST_KEY"] == "present"
    finally:
        del os.environ["WRAPTURE_TEST_KEY"]


def test_a_mapping_binding_updates_content_in_place() -> None:
    class Config:
        SETTINGS = {"currency": "USD", "tax_rate": 0.2}

    holder = Config.SETTINGS

    @bound(Config, "SETTINGS", mode="mapping").updates({"tax_rate": 0.0})
    def body(SETTINGS: Any) -> None:
        assert holder == {"currency": "USD", "tax_rate": 0.0}

    body()
    assert holder == {"currency": "USD", "tax_rate": 0.2}


# ---------------------------------------------------------------------------
# alias derivation
# ---------------------------------------------------------------------------


def test_the_alias_is_the_final_segment_of_the_addressing_path() -> None:
    class App:
        gateway = Gateway()

    @taped()
    @bound(App, "gateway.charge").on_call.returns({"id": "STUB"})
    def body(tape: Any, charge: Any) -> None:
        assert App.gateway.charge(1) == {"id": "STUB"}

    body()


def test_alias_overrides_the_derived_name() -> None:
    @bound(Gateway, "charge", alias="gateway_charge").on_call.returns({"id": "STUB"})
    def body(gateway_charge: Any) -> None:
        assert Gateway().charge(1) == {"id": "STUB"}

    body()


def test_a_non_identifier_slot_name_requires_alias() -> None:
    registry: dict[str, Any] = {"content-type": lambda: "text"}

    with pytest.raises(TypeError, match="pass alias="):

        @bound(registry, item="content-type").overrides("json")
        def body(**kwargs: Any) -> None: ...

    @bound(registry, item="content-type", alias="content_type").overrides("json")
    def body2(content_type: Any) -> None:
        assert registry["content-type"] == "json"

    body2()


def test_colliding_aliases_across_decorators_error() -> None:
    class Channel:
        def close(self) -> None: ...

    class Transport:
        def close(self) -> None: ...

    with pytest.raises(TypeError, match="already injected"):

        @bound(Transport, "close")
        @bound(Channel, "close")
        def body(close: Any, **kwargs: Any) -> None: ...

    @taped()
    @bound(Transport, "close", alias="transport_close")
    @bound(Channel, "close", alias="channel_close")
    def body2(tape: Any, transport_close: Any, channel_close: Any) -> None:
        Channel().close()
        Transport().close()

        tape.assert_order(channel_close, transport_close)

    body2()


def test_a_missing_parameter_without_kwargs_errors_at_decoration() -> None:
    with pytest.raises(TypeError, match="no parameter of that name"):

        @bound(Gateway, "charge")
        def body() -> None: ...


# ---------------------------------------------------------------------------
# signature pruning
# ---------------------------------------------------------------------------


def test_each_layer_prunes_only_its_own_alias() -> None:
    @taped()
    @bound(Gateway, "charge")
    def body(where: str, tape: Any, charge: Any) -> None: ...

    assert list(inspect.signature(body).parameters) == ["where"]


def test_var_keyword_injection_needs_no_pruning() -> None:
    @bound(Gateway, "charge")
    def body(where: str, **kwargs: Any) -> None: ...

    assert list(inspect.signature(body).parameters) == ["where", "kwargs"]


# ---------------------------------------------------------------------------
# collapsing decorators on the same target
# ---------------------------------------------------------------------------


def test_same_target_decorators_collapse_to_one_binding() -> None:
    @taped()
    @bound(Model, "status").on_get.returns(5)
    @bound(Model, "status").on_set.raises(AttributeError("read-only"))
    def body(tape: Any, status: Any) -> None:
        model = Model()
        assert model.status == 5

        with pytest.raises(AttributeError, match="read-only"):
            model.status = 9

        status.events.assert_times(2)

    body()


def test_collapsed_stages_compose_in_reading_order() -> None:
    class Service:
        def word(self) -> Any:
            return "real"

    @bound(Service, "word").on_call.transforms_result(lambda r: r + "!")
    @bound(Service, "word").on_call.transforms_result(str.upper).returns("ab")
    def body(word: Any) -> None:
        # Reading order: the suffix stage is outermost, then upper, then
        # the terminal, the same as those statements on a live binding.
        assert Service().word() == "AB!"

    body()


def test_collapsed_terminals_are_last_in_reading_order_wins() -> None:
    @bound(Gateway, "charge").on_call.returns({"id": "upper"})
    @bound(Gateway, "charge").on_call.returns({"id": "lower"})
    def body(charge: Any) -> None:
        assert Gateway().charge(1) == {"id": "lower"}

    body()


def test_distinct_aliases_do_not_collapse() -> None:
    @taped()
    @bound(Gateway, "charge", alias="outer")
    @bound(Gateway, "charge", alias="inner").on_call.returns({"id": "STUB"})
    def body(tape: Any, outer: Any, inner: Any) -> None:
        assert outer is not inner
        assert Gateway().charge(1) == {"id": "STUB"}

    body()


def test_equivalent_but_differently_spelled_addressing_does_not_merge() -> None:
    with pytest.raises(TypeError, match="already injected"):

        @bound(Gateway, "charge")
        @bound("tests.test_decorators", "Gateway.charge")
        def body(charge: Any) -> None: ...


# ---------------------------------------------------------------------------
# convention checks
# ---------------------------------------------------------------------------


def test_generator_functions_are_rejected() -> None:
    with pytest.raises(TypeError, match="generator function"):

        @bound(Gateway, "charge")
        def gen(charge: Any) -> Any:
            yield

    with pytest.raises(TypeError, match="generator function"):

        @taped()
        async def agen(tape: Any) -> Any:
            yield


def test_a_pytest_fixture_below_the_decorator_is_rejected() -> None:
    with pytest.raises(TypeError, match="fixture"):

        @bound(Gateway, "charge")
        @pytest.fixture
        def fix(charge: Any) -> None: ...


# ---------------------------------------------------------------------------
# async tests
# ---------------------------------------------------------------------------


def test_the_binding_spans_the_await() -> None:
    @taped()
    @bound(PushClient, "send").on_call.returns("queued")
    async def body(tape: Any, send: Any) -> None:
        assert await PushClient().send("ana") == "queued"

        send.events.finished().assert_once()

    assert inspect.iscoroutinefunction(body)
    asyncio.run(body())


def test_async_teardown_runs_after_the_body() -> None:
    order: list[str] = []

    @bound(Gateway, "charge").on_call.returns({"id": "STUB"})
    async def body(charge: Any) -> None:
        order.append("body")
        assert Gateway().charge(1) == {"id": "STUB"}

    asyncio.run(body())
    order.append("after")

    assert order == ["body", "after"]
    assert Gateway().charge(1) == {"id": "ch_1", "amount": 1}


# ---------------------------------------------------------------------------
# unittest.TestCase methods
# ---------------------------------------------------------------------------


def test_testcase_methods_are_supported() -> None:
    class Case(unittest.TestCase):
        @taped()
        @bound(Gateway, "charge").on_call.returns({"id": "STUB"})
        def test_it(self, tape: Any, charge: Any) -> None:
            assert isinstance(self, Case)
            assert Gateway().charge(1) == {"id": "STUB"}
            charge.events.assert_once()

    result = unittest.TestResult()
    Case("test_it").run(result)

    assert result.wasSuccessful(), result.errors + result.failures


def test_isolated_asyncio_testcase_methods_are_supported() -> None:
    class Case(unittest.IsolatedAsyncioTestCase):
        @taped()
        @bound(PushClient, "send").on_call.returns("queued")
        async def test_it(self, tape: Any, send: Any) -> None:
            assert await PushClient().send("ana") == "queued"
            send.events.finished().assert_once()

    result = unittest.TestResult()
    Case("test_it").run(result)

    assert result.wasSuccessful(), result.errors + result.failures


# ---------------------------------------------------------------------------
# taped()
# ---------------------------------------------------------------------------


def test_taped_applies_the_bindings_it_is_given() -> None:
    charge = binding(Gateway, "charge").on_call.returns({"id": "STUB"})

    @taped(charge)
    def body(tape: Any) -> None:
        assert Gateway().charge(1) == {"id": "STUB"}
        tape.assert_order(charge)

    body()
    assert Gateway().charge(1) == {"id": "ch_1", "amount": 1}


def test_taped_alias_renames_the_injected_tape() -> None:
    @taped(alias="inner")
    def body(inner: Any) -> None:
        assert inner.pending == 0

    body()


def test_taped_nests_under_an_ambient_tape() -> None:
    charge = binding(Gateway, "charge").on_call.returns({"id": "STUB"})

    @taped()
    def body(tape: Any) -> None:
        Gateway().charge(1)
        tape.for_binding(charge).assert_once()

    with charge, timeline() as ambient:
        body()

        ambient.for_binding(charge).assert_once()


def test_a_spec_in_a_variable_is_reusable_across_functions() -> None:
    stubbed = bound(Gateway, "charge").on_call.returns({"id": "STUB"})

    @taped()
    @stubbed
    def one(tape: Any, charge: Any) -> None:
        Gateway().charge(1)
        charge.events.assert_once()

    @taped()
    @stubbed
    def two(tape: Any, charge: Any) -> None:
        Gateway().charge(2)
        charge.events.assert_once()

    one()
    two()
