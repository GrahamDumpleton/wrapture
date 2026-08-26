"""Tests for finding a binding you do not hold: find_binding(),
find_bindings(), binding_of(), bindings_of() and Tape.where()."""

import functools
import os
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

import wrapture
from wrapture import (
    AmbiguousBindingError,
    Instrumentation,
    NoBindingError,
    binding,
    binding_of,
    bindings_of,
    find_binding,
    find_bindings,
    instrumentation_hook,
    observed,
    timeline,
)


class Gateway:
    def charge(self, amount: int) -> dict[str, Any]:
        return {"id": f"ch_{amount}", "amount": amount}

    def refund(self, charge_id: str) -> dict[str, Any]:
        return {"refunded": charge_id}


class Ledger:
    rate = 0.05

    def record(self, entry: str) -> str:
        return f"led-{entry}"


HANDLERS: dict[str, Any] = {"GET": lambda: "got"}

_PATH = f"{__name__}:Gateway.charge"


def application(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


# ---------------------------------------------------------------------------
# find_bindings / find_binding: by location
# ---------------------------------------------------------------------------


def test_find_binding_by_location_returns_the_applied_binding() -> None:
    charge = binding(Gateway, "charge")

    with charge:
        assert find_binding(Gateway, "charge") is charge
        assert find_bindings(Gateway, "charge") == [charge]


def test_every_spelling_of_a_location_finds_the_same_binding() -> None:
    with binding(Gateway, "charge") as charge:
        assert find_binding(_PATH) is charge
        assert find_binding(__name__, "Gateway.charge") is charge
        assert find_binding(__name__, "Gateway", "charge") is charge
        assert find_binding(f"{__name__}:Gateway", "charge") is charge
        assert find_binding(Gateway(), "charge") is charge


def test_lookup_never_imports_the_module_it_is_asked_about() -> None:
    assert "cfgl_never_imported" not in sys.modules

    assert find_bindings("cfgl_never_imported:Thing.method") == []
    assert "cfgl_never_imported" not in sys.modules


def test_results_are_live_and_only_applied_bindings_are_found() -> None:
    charge = binding(Gateway, "charge")

    assert find_bindings(Gateway, "charge") == []

    charge.apply()
    assert find_bindings(Gateway, "charge") == [charge]

    charge.suspend()
    assert find_bindings(Gateway, "charge") == [charge]

    charge.remove()
    assert find_bindings(Gateway, "charge") == []

    with pytest.raises(NoBindingError, match="no applied binding matches"):
        find_binding(Gateway, "charge")


def test_a_different_location_is_not_found() -> None:
    with binding(Gateway, "charge"):
        assert find_bindings(Gateway, "refund") == []
        assert find_bindings(Ledger, "record") == []


def test_stacked_bindings_are_ambiguous_for_the_singular_form() -> None:
    inner = binding(Gateway, "charge", label="recorder")
    outer = binding(Gateway, "charge").on_call.returns(None)

    with inner, outer:
        # Plural: in order of application, outermost last.

        assert find_bindings(Gateway, "charge") == [inner, outer._binding]

        with pytest.raises(AmbiguousBindingError) as info:
            find_binding(Gateway, "charge")

        message = str(info.value)
        assert "2 bindings match" in message
        assert "recorder" in message
        assert _PATH in message
        assert "Add a label" in message

        # A label singles one out.

        assert find_binding(Gateway, "charge", label="recorder") is inner


def test_the_application_order_survives_reapplication() -> None:
    first = binding(Gateway, "charge").apply()
    second = binding(Gateway, "charge").apply()

    first.remove()
    first.apply()

    try:
        assert find_bindings(Gateway, "charge") == [second, first]
    finally:
        first.remove()
        second.remove()


def test_slot_bindings_are_found_with_attr_and_item() -> None:
    holder = binding(Ledger, attr="rate").overrides(2)
    entry = binding(HANDLERS, item="GET", mode="callable")
    environ = binding(os, "environ", item="CFGL_SEEN").overrides("1")

    with holder, entry, environ:
        assert find_binding(Ledger, attr="rate") is holder
        assert find_binding(HANDLERS, item="GET") is entry
        assert find_binding(os, "environ", item="CFGL_SEEN") is environ

        # An attribute slot has the path an attribute binding on the
        # same name would have, so the positional spelling finds it too.

        assert find_binding(Ledger, "rate") is holder


def test_a_mapping_binding_on_the_mapping_itself_is_found_bare() -> None:
    settings: dict[str, Any] = {"a": 1}

    with binding(settings, mode="mapping").updates({"b": 2}) as substitute:
        assert find_binding(settings) is substitute


def test_attribute_and_request_mode_bindings_are_found() -> None:
    rate = binding(Ledger, "rate")
    app = binding(__name__, "application", mode="wsgi")

    with rate, app:
        assert find_binding(Ledger, "rate") is rate
        assert find_binding(__name__, "application") is app


# ---------------------------------------------------------------------------
# find_bindings / find_binding: by label
# ---------------------------------------------------------------------------


def test_find_by_label_matches_the_assigned_label() -> None:
    with binding(Gateway, "charge", label="gateway.charge") as charge:
        assert find_binding(label="gateway.charge") is charge
        assert find_bindings(label=_PATH) == []


def test_find_by_label_falls_back_to_the_path_when_unlabelled() -> None:
    with binding(Gateway, "charge") as charge:
        assert find_binding(label=_PATH) is charge


def test_a_label_is_matched_exactly_not_as_a_pattern() -> None:
    with binding(Gateway, "charge", label="gateway.charge"):
        assert find_bindings(label="gateway.*") == []
        assert find_bindings(label="gateway") == []


def test_location_and_label_must_both_match() -> None:
    with binding(Gateway, "charge", label="gateway.charge") as charge:
        assert find_binding(Gateway, "charge", label="gateway.charge") is charge
        assert find_bindings(Gateway, "charge", label="other") == []
        assert find_bindings(Gateway, "refund", label="gateway.charge") == []


def test_the_error_names_both_halves_of_the_query() -> None:
    with pytest.raises(NoBindingError, match=rf"{_PATH} with label 'nope'"):
        find_binding(Gateway, "charge", label="nope")


def test_a_query_needs_a_location_or_a_label() -> None:
    with pytest.raises(ValueError, match="needs a location, a label, or both"):
        find_bindings()

    with pytest.raises(TypeError, match="attr= or item=, not both"):
        find_bindings(Ledger, attr="rate", item="x")

    with pytest.raises(TypeError, match="needs a target"):
        find_bindings(label="x", attr="rate")


# ---------------------------------------------------------------------------
# observed() proxies take part
# ---------------------------------------------------------------------------


def test_an_observed_proxy_is_found_by_label_and_by_path() -> None:
    @observed(label="hook")
    def hook() -> str:
        return "ran"

    plain = observed(application)

    assert find_binding(label="hook") is hook
    assert find_binding(hook.path) is hook
    assert find_binding(__name__, "application") is plain
    assert find_binding(label=f"{__name__}:application") is plain


def test_a_dropped_observed_proxy_is_forgotten() -> None:
    def make() -> None:
        observed(application, label="transient")

    make()

    assert find_bindings(label="transient") == []


# ---------------------------------------------------------------------------
# binding_of / bindings_of
# ---------------------------------------------------------------------------


def test_binding_of_recognises_every_way_of_holding_the_wrapper() -> None:
    charge = binding(Gateway, "charge")

    assert binding_of(Gateway.charge) is None
    assert bindings_of(Gateway.charge) == []

    with charge:
        assert binding_of(vars(Gateway)["charge"]) is charge
        assert binding_of(Gateway.charge) is charge
        assert binding_of(Gateway().charge) is charge

        def decorator(fn: Any) -> Any:
            @functools.wraps(fn)
            def inner(*args: Any, **kwargs: Any) -> Any:
                return fn(*args, **kwargs)

            return inner

        assert binding_of(decorator(vars(Gateway)["charge"])) is charge
        assert binding_of(vars(Gateway)["refund"]) is None


def test_bindings_of_lists_stacked_layers_outermost_first() -> None:
    inner = binding(Gateway, "charge", label="inner")
    outer = binding(Gateway, "charge", label="outer")

    with inner, outer:
        assert bindings_of(Gateway().charge) == [outer, inner]
        assert binding_of(Gateway().charge) is outer


def test_binding_of_still_recognises_a_retired_wrapper() -> None:
    charge = binding(Gateway, "charge").apply()
    copy = vars(Gateway)["charge"]
    charge.remove()

    assert binding_of(copy) is charge
    assert charge.removed
    assert binding_of(vars(Gateway)["charge"]) is None


def test_binding_of_an_observed_function_is_the_proxy_itself() -> None:
    @observed
    def hook() -> None:
        pass

    assert binding_of(hook) is hook

    charge = binding(Gateway, "charge")

    with charge:
        # An observed layer over a bound wrapper lists both.

        layered = observed(vars(Gateway)["charge"], label="layered")
        assert bindings_of(layered) == [layered, charge]


def test_binding_of_an_attribute_descriptor_and_a_wsgi_app() -> None:
    rate = binding(Ledger, "rate")
    app = binding(__name__, "application", mode="wsgi")

    with rate, app:
        assert binding_of(vars(Ledger)["rate"]) is rate
        assert binding_of(sys.modules[__name__].application) is app

    # Standalone middleware, built with no binding, belongs to none.

    assert binding_of(wrapture.WSGIMiddleware(application)) is None


def test_binding_of_a_plain_object_is_none() -> None:
    assert binding_of(application) is None
    assert binding_of(42) is None
    assert bindings_of(Gateway) == []


# ---------------------------------------------------------------------------
# the motivating case: bindings applied by an instrumentation
# ---------------------------------------------------------------------------


class Shop(Instrumentation):
    target = "cfgl_shop"
    removable = True

    @instrumentation_hook("cfgl_shop")
    def shop(self, name: str, module: Any) -> None:
        charge = wrapture.binding(module.Gateway, "charge", label="shop.charge").apply()
        record = wrapture.binding(module.Ledger, "record").apply()

        self.on_cleanup(charge.remove)
        self.on_cleanup(record.remove)


@pytest.fixture
def shop_module() -> Iterator[types.ModuleType]:
    module = types.ModuleType("cfgl_shop")
    module.Gateway = Gateway  # type: ignore[attr-defined]
    module.Ledger = Ledger  # type: ignore[attr-defined]
    sys.modules["cfgl_shop"] = module

    yield module

    del sys.modules["cfgl_shop"]


def test_asserting_on_an_instrumentations_bindings(
    shop_module: types.ModuleType,
) -> None:
    def place_order() -> None:
        Gateway().charge(5)
        Ledger().record("ch_5")

    with wrapture.instrumentation(Shop):
        charge = find_binding(label="shop.charge")
        record = find_binding(Ledger, "record")

        with timeline() as tape:
            place_order()

            charge.events.with_args(amount=5).assert_once()
            record.events.assert_once()
            tape.assert_order(charge, record)

    # The scope has ended; the bindings are gone from the lookup.

    assert find_bindings(label="shop.charge") == []


# ---------------------------------------------------------------------------
# Tape.where
# ---------------------------------------------------------------------------


def test_where_selects_events_by_path_and_by_display_label() -> None:
    charge = binding(Gateway, "charge", label="gateway.charge")
    record = binding(Ledger, "record")

    with timeline(charge, record) as tape:
        Gateway().charge(1)
        Ledger().record("x")

        by_path = tape.where(path=_PATH)
        assert by_path.count == 1
        assert by_path.label == _PATH

        by_label = tape.where(label="gateway.charge")
        assert by_label.count == 1
        assert by_label.label == "gateway.charge"

        # An unlabelled binding shows under its path.

        assert tape.where(label=f"{__name__}:Ledger.record").count == 1
        assert tape.where(path=_PATH, label="gateway.charge").count == 1
        assert tape.where(path=_PATH, label="other").count == 0
        assert tape.where(path="nowhere").count == 0


def test_where_needs_a_path_or_a_label() -> None:
    with timeline() as tape:
        with pytest.raises(ValueError, match="needs a path, a label, or both"):
            tape.where()
