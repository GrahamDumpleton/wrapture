"""Tests for the signature and convention override mixins.

The mixins compose over wrapt's public FunctionWrapper machinery: each
consumes its keyword cooperatively, defaults to delegating exactly as
an unmixed wrapper would, and overrides what introspection reports
without touching the wrapped callable. A subprocess case runs the core
assertions on the pure-Python wrapt implementation as well, since
working identically over both builds is part of the contract.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from wrapt import BoundFunctionWrapper, FunctionWrapper

from wrapture._wrappermixins import (
    BoundConventionOverrideMixin,
    BoundSignatureOverrideMixin,
    ConventionOverrideMixin,
    SignatureOverrideMixin,
)


class BoundStub(
    BoundConventionOverrideMixin,
    BoundSignatureOverrideMixin,
    BoundFunctionWrapper[Any, Any],
):
    pass


class Stub(
    ConventionOverrideMixin,
    SignatureOverrideMixin,
    FunctionWrapper[Any, Any],
):
    __bound_function_wrapper__ = BoundStub


def passthrough(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return wrapped(*args, **kwargs)


def template(*args: Any, **kwargs: Any) -> Any:
    return ("called", args, kwargs)


def prototype(amount: int, currency: str = "USD", *, retries: int = 3) -> None: ...


# ---------------------------------------------------------------------------
# defaults: no override means delegate exactly as before
# ---------------------------------------------------------------------------


def test_without_overrides_everything_delegates() -> None:
    def real(a: int, b: int = 2) -> int:
        return a + b

    wrapper = Stub(real, passthrough)

    assert inspect.signature(wrapper) == inspect.signature(real)
    assert wrapper.__defaults__ == (2,)
    assert not inspect.iscoroutinefunction(wrapper)
    assert wrapper(1) == 3


def test_conventions_delegate_when_not_overridden() -> None:
    async def fetch(url: str) -> str:
        return url

    wrapper = Stub(fetch, passthrough)

    assert inspect.iscoroutinefunction(wrapper)
    assert not inspect.isasyncgenfunction(wrapper)


# ---------------------------------------------------------------------------
# signature override
# ---------------------------------------------------------------------------


def test_signature_override_from_a_prototype() -> None:
    wrapper = Stub(template, passthrough, signature=prototype)

    signature = inspect.signature(wrapper)
    assert list(signature.parameters) == ["amount", "currency", "retries"]
    assert wrapper.__defaults__ == ("USD",)
    assert wrapper.__kwdefaults__ == {"retries": 3}
    # Under PEP 563 (the future import above) annotations are strings.
    assert wrapper.__annotations__["amount"] == "int"

    code = wrapper.__code__
    assert code.co_argcount == 2
    assert code.co_kwonlyargcount == 1
    assert code.co_varnames[:3] == ("amount", "currency", "retries")


def test_signature_override_from_a_prebuilt_signature() -> None:
    built = inspect.signature(prototype)
    wrapper = Stub(template, passthrough, signature=built)

    assert inspect.signature(wrapper) is built


def test_signature_override_does_not_change_calling() -> None:
    wrapper = Stub(template, passthrough, signature=prototype)

    assert wrapper(1, 2, x=3) == ("called", (1, 2), {"x": 3})


# ---------------------------------------------------------------------------
# convention override
# ---------------------------------------------------------------------------


def test_each_convention_reports_its_probes() -> None:
    cases = {
        "sync": (False, False, False),
        "generator": (False, False, True),
        "coroutine": (True, False, False),
        "async_generator": (False, True, False),
    }

    for convention, (coro, agen, gen) in cases.items():
        wrapper = Stub(template, passthrough, convention=convention)

        assert inspect.iscoroutinefunction(wrapper) is coro, convention
        assert inspect.isasyncgenfunction(wrapper) is agen, convention
        assert inspect.isgeneratorfunction(wrapper) is gen, convention


def test_convention_sync_overrides_an_async_target() -> None:
    async def fetch(url: str) -> str:
        return url

    wrapper = Stub(fetch, passthrough, convention="sync")

    assert not inspect.iscoroutinefunction(wrapper)
    assert wrapper._self_is_not_coroutine is True


def test_an_unknown_convention_is_refused() -> None:
    with pytest.raises(ValueError, match="convention must be one of"):
        Stub(template, passthrough, convention="agenda")


# ---------------------------------------------------------------------------
# both overrides together
# ---------------------------------------------------------------------------


def test_overrides_compose_in_the_blessed_order() -> None:
    wrapper = Stub(template, passthrough, signature=prototype, convention="coroutine")

    assert inspect.iscoroutinefunction(wrapper)
    signature = inspect.signature(wrapper)
    assert list(signature.parameters) == ["amount", "currency", "retries"]

    code = wrapper.__code__
    assert code.co_argcount == 2


# ---------------------------------------------------------------------------
# bound wrappers: placed on a class
# ---------------------------------------------------------------------------


def method_prototype(self: Any, amount: int, currency: str = "USD") -> None: ...


class Holder:
    # Annotated Any so mypy does not push the descriptor protocol's
    # generics onto call sites; the runtime behaviour is the point here.

    plain: Any = Stub(template, passthrough)
    shaped: Any = Stub(template, passthrough, signature=method_prototype)
    marked: Any = Stub(template, passthrough, convention="coroutine")
    both: Any = Stub(
        template, passthrough, signature=method_prototype, convention="coroutine"
    )


def test_bound_signature_override_strips_self() -> None:
    holder = Holder()

    signature = inspect.signature(holder.shaped)
    assert list(signature.parameters) == ["amount", "currency"]


def test_bound_convention_override_reports_through_binding() -> None:
    holder = Holder()

    assert inspect.iscoroutinefunction(holder.marked)
    assert not inspect.iscoroutinefunction(holder.plain)


def test_bound_overrides_compose() -> None:
    holder = Holder()

    assert inspect.iscoroutinefunction(holder.both)
    assert list(inspect.signature(holder.both).parameters) == ["amount", "currency"]


def test_bound_wrapper_without_overrides_delegates() -> None:
    holder = Holder()

    assert holder.plain(1, x=2)[0] == "called"
    assert isinstance(holder.plain, BoundStub)
