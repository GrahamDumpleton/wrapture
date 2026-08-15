"""Tests for the behaviour vocabulary and the behaviour pipeline."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from wrapture import NotImplementedYetError, binding


class Gateway:
    def charge(self, amount: int, currency: str = "USD") -> dict[str, Any]:
        return {"id": f"ch_{amount}", "amount": amount}


class Ledger:
    rate = 0.05  # data attribute: detected as attribute mode

    def record(self, entry: str) -> str:
        return f"led-{entry}"


class AsyncSvc:
    async def inner(self, n: int) -> int:
        await asyncio.sleep(0)
        return n * 2


# ---------------------------------------------------------------------------
# behaviour vocabulary
# ---------------------------------------------------------------------------


def test_returns_replaces_the_result() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge").on_call.returns({"id": "STUB"}).apply()
    try:
        assert gw.charge(1) == {"id": "STUB"}
    finally:
        bnd.remove()


def test_raises_injects_a_failure() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge").on_call.raises(TimeoutError("down")).apply()
    try:
        with pytest.raises(TimeoutError):
            gw.charge(1)
    finally:
        bnd.remove()


def test_returns_and_raises_never_call_the_original() -> None:
    calls: list[int] = []

    class Service:
        def go(self, n: int) -> int:
            calls.append(n)
            return n

    bnd = binding(Service, "go").on_call.returns(0).apply()
    try:
        assert Service().go(1) == 0
    finally:
        bnd.remove()

    bnd = binding(Service, "go").on_call.raises(TimeoutError("down")).apply()
    try:
        with pytest.raises(TimeoutError):
            Service().go(2)
    finally:
        bnd.remove()

    assert calls == []


def test_decorates_can_run_the_original_and_then_raise() -> None:
    calls: list[int] = []

    class Service:
        def go(self, n: int) -> int:
            calls.append(n)
            return n

    def go_then_drop(
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        wrapped(*args, **kwargs)
        raise TimeoutError("response lost")

    bnd = binding(Service, "go").on_call.decorates(go_then_drop).apply()
    try:
        with pytest.raises(TimeoutError):
            Service().go(1)
        assert calls == [1]  # the original really ran first
    finally:
        bnd.remove()


def test_decorates_uses_the_wrapt_signature() -> None:
    gw = Gateway()

    def around(
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = wrapped(*args, **kwargs)
        result["traced"] = type(instance).__name__
        return result

    bnd = binding(Gateway, "charge").on_call.decorates(around).apply()
    try:
        assert gw.charge(50)["traced"] == "Gateway"
    finally:
        bnd.remove()


def test_transforms_args_rewrites_the_inbound_call() -> None:
    gw = Gateway()
    bnd = (
        binding(Gateway, "charge")
        .on_call.transforms_args(lambda a, k: ((a[0] * 100,), k))
        .apply()
    )
    try:
        assert gw.charge(5)["amount"] == 500
    finally:
        bnd.remove()


def test_transforms_result_rewrites_what_came_back() -> None:
    gw = Gateway()
    bnd = (
        binding(Gateway, "charge").on_call.transforms_result(lambda r: r["id"]).apply()
    )
    try:
        result: Any = gw.charge(7)
        assert result == "ch_7"
    finally:
        bnd.remove()


def test_validates_args_can_reject() -> None:
    gw = Gateway()

    def positive(amount: int, currency: str = "USD") -> None:
        assert amount > 0, f"non-positive: {amount}"

    bnd = binding(Gateway, "charge").on_call.validates_args(positive).apply()
    try:
        assert gw.charge(3)["amount"] == 3
        with pytest.raises(AssertionError, match="non-positive"):
            gw.charge(-1)
    finally:
        bnd.remove()


def test_validates_args_and_result_both_apply() -> None:
    gw = Gateway()
    seen: list[tuple[str, int]] = []
    bnd = binding(Gateway, "charge")
    bnd.on_call.validates_args(
        lambda amount, currency="USD": seen.append(("in", amount))
    )
    bnd.on_call.validates_result(lambda r: seen.append(("out", r["amount"])))
    bnd.apply()
    try:
        gw.charge(7)
        assert seen == [("in", 7), ("out", 7)]
    finally:
        bnd.remove()


def test_target_is_fully_restored_after_remove() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge").on_call.returns({"id": "STUB"}).apply()
    bnd.remove()
    assert gw.charge(1) == {"id": "ch_1", "amount": 1}


def test_behaviour_can_be_reconfigured_while_patched() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge").apply()
    try:
        for value in ("A", "B", "C"):
            bnd.on_call.returns({"id": value})
            assert gw.charge(1)["id"] == value
        bnd.on_call.passes_through()
        assert gw.charge(1)["id"] == "ch_1"
        assert bnd.active  # patch survives passes_through()
    finally:
        bnd.remove()


def test_behaviour_setters_return_the_binding_so_apply_chains() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge").on_call.returns({"id": "STUB"}).apply()
    try:
        assert gw.charge(1) == {"id": "STUB"}
    finally:
        bnd.remove()


# ---------------------------------------------------------------------------
# behaviour pipeline
# ---------------------------------------------------------------------------


def test_transforms_compose_in_either_order() -> None:
    gw = Gateway()

    for args_first in (True, False):
        bnd = binding(Gateway, "charge")
        if args_first:
            bnd.on_call.transforms_args(lambda a, k: ((a[0] * 100,), k))
            bnd.on_call.transforms_result(lambda r: {**r, "seen": True})
        else:
            bnd.on_call.transforms_result(lambda r: {**r, "seen": True})
            bnd.on_call.transforms_args(lambda a, k: ((a[0] * 100,), k))
        bnd.apply()
        try:
            result = gw.charge(5)
            assert result["amount"] == 500
            assert result["seen"] is True
        finally:
            bnd.remove()


def test_terminal_replaces_terminal_but_stages_persist() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge")
    bnd.on_call.transforms_result(lambda r: {**r, "tag": 1})
    bnd.on_call.returns({"amount": 0})
    bnd.apply()
    try:
        assert gw.charge(9) == {"amount": 0, "tag": 1}
        bnd.on_call.raises(ValueError("nope"))
        with pytest.raises(ValueError):
            gw.charge(9)
    finally:
        bnd.remove()


def test_passes_through_clears_terminal_and_stages() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge")
    bnd.on_call.transforms_args(lambda a, k: ((a[0] * 2,), k))
    bnd.on_call.returns({"amount": -1})
    bnd.apply()
    try:
        assert gw.charge(4) == {"amount": -1}
        bnd.on_call.passes_through()
        assert gw.charge(4) == {"id": "ch_4", "amount": 4}
    finally:
        bnd.remove()


# ---------------------------------------------------------------------------
# async targets
# ---------------------------------------------------------------------------


def test_transforms_result_on_an_async_target() -> None:
    bnd = (
        binding(AsyncSvc, "inner").on_call.transforms_result(lambda r: r + 1000).apply()
    )
    try:
        assert asyncio.run(AsyncSvc().inner(1)) == 1002
    finally:
        bnd.remove()


def test_validates_result_on_an_async_target() -> None:
    seen: list[int] = []
    bnd = binding(AsyncSvc, "inner").on_call.validates_result(seen.append).apply()
    try:
        assert asyncio.run(AsyncSvc().inner(3)) == 6
        assert seen == [6]  # the value, not a coroutine
    finally:
        bnd.remove()


def test_argument_and_result_stages_compose_on_an_async_target() -> None:
    bnd = binding(AsyncSvc, "inner")
    bnd.on_call.transforms_args(lambda a, k: ((a[0] * 10,), k))
    bnd.on_call.transforms_result(lambda r: r + 1)
    bnd.apply()
    try:
        assert asyncio.run(AsyncSvc().inner(2)) == 41
    finally:
        bnd.remove()


# ---------------------------------------------------------------------------
# attribute behaviours are stubbed loudly
# ---------------------------------------------------------------------------


def test_attribute_behaviours_are_stubbed_loudly() -> None:
    attr = binding(Ledger, "rate")

    calls: list[Callable[[], Any]] = [
        lambda: attr.on_get.returns(1),
        lambda: attr.on_get.transforms(str),
        lambda: attr.on_get.wraps_value(),
        lambda: attr.on_get.validates(None),
        lambda: attr.on_get.decorates(str),
        lambda: attr.on_get.raises(ValueError()),
        lambda: attr.on_get.passes_through(),
        lambda: attr.on_set.transforms(str),
        lambda: attr.on_set.rejects(),
        lambda: attr.on_delete.rejects(),
    ]
    for call in calls:
        with pytest.raises(NotImplementedYetError):
            call()
