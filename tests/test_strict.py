"""Tests for strict signature checking of stubbed calls."""

import asyncio
from typing import Any

import pytest

from wrapture import binding, discover, timeline


class Gateway:
    # Deliberately bad calls are the point of these tests, so instances
    # are held as Any and mypy is not asked to check the call shapes.

    def charge(self, amount: int, currency: str = "USD") -> Any:
        return {"id": f"ch_{amount}", "amount": amount, "currency": currency}

    async def acharge(self, amount: int) -> Any:
        return {"id": f"ch_{amount}"}

    def anything(self, *args: Any, **kwargs: Any) -> Any:
        return "real"


def make() -> Any:
    return Gateway()


def test_a_stub_rejects_a_call_that_does_not_fit_the_signature() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("stub")

    with charge:
        gateway: Any = Gateway()
        assert gateway.charge(1) == "stub"
        assert gateway.charge(1, currency="EUR") == "stub"

        with pytest.raises(TypeError, match=r"Gateway.charge \(stubbed\): .*bogus"):
            gateway.charge(1, bogus=4)

        with pytest.raises(TypeError, match="stubbed"):
            gateway.charge()

        with pytest.raises(TypeError, match="stubbed"):
            gateway.charge(1, "EUR", "extra")


@pytest.mark.parametrize("terminal", ["returns", "raises", "returns_from", "decorates"])
def test_every_terminal_is_checked(terminal: str) -> None:
    charge = binding(Gateway, "charge")

    if terminal == "returns":
        charge.on_call.returns("stub")
    elif terminal == "raises":
        charge.on_call.raises(TimeoutError("down"))
    elif terminal == "returns_from":
        charge.on_call.returns_from(iter(["a", "b"]))
    else:
        charge.on_call.decorates(lambda wrapped, instance, args, kwargs: "fabricated")

    with charge:
        with pytest.raises(TypeError, match="stubbed"):
            make().charge(1, bogus=4)


def test_pass_through_and_stage_only_pipelines_are_left_to_the_real_callable() -> None:
    charge = binding(Gateway, "charge")

    # a transform that adapts the call shape is legitimate when the real
    # callable runs, and the real callable then does the checking

    def strip_test_flag(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        kwargs = dict(kwargs)
        kwargs.pop("test_only", None)
        return args, kwargs

    charge.on_call.transforms_args(strip_test_flag)

    with charge:
        gateway: Any = Gateway()
        assert gateway.charge(1, test_only=True)["id"] == "ch_1"

        with pytest.raises(TypeError) as excinfo:
            gateway.charge(1, bogus=4)

        assert "stubbed" not in str(excinfo.value)


def test_strict_false_opts_out() -> None:
    charge = binding(Gateway, "charge", strict=False)
    charge.on_call.returns("stub")

    with charge:
        assert make().charge(1, bogus=4) == "stub"


def test_a_widening_decorator_needs_strict_false() -> None:
    def with_flag(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        kwargs = dict(kwargs)
        flag = kwargs.pop("dry_run", False)
        return "skipped" if flag else wrapped(*args, **kwargs)

    checked = binding(Gateway, "charge")
    checked.on_call.decorates(with_flag)

    with checked:
        with pytest.raises(TypeError, match="stubbed"):
            make().charge(1, dry_run=True)

    widened = binding(Gateway, "charge", strict=False)
    widened.on_call.decorates(with_flag)

    with widened:
        assert make().charge(1, dry_run=True) == "skipped"
        assert make().charge(1)["id"] == "ch_1"


def test_a_var_args_target_accepts_anything() -> None:
    anything = binding(Gateway, "anything")
    anything.on_call.returns("stub")

    with anything:
        assert make().anything(1, 2, x=3) == "stub"


def test_a_call_through_the_class_is_checked_against_the_bound_signature() -> None:
    # Calling Gateway.charge(gateway, 1) rather than gateway.charge(1)
    # hands the wrapper a wrapt partial with the instance bound, whose
    # signature (from wrapt 2.4.1) no longer lists self, so the check
    # fits the call as the instance-call path does and still rejects
    # what does not fit.

    seen: list[tuple[Any, Any]] = []

    def handler(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> str:
        seen.append((args, kwargs))
        return "fabricated"

    charge = binding(Gateway, "charge")
    charge.on_call.decorates(handler)

    with charge:
        gateway = Gateway()
        through_class: Any = Gateway.charge
        assert through_class(gateway, 1) == "fabricated"
        assert through_class(gateway, 1, currency="EUR") == "fabricated"

        with pytest.raises(TypeError, match=r"Gateway.charge \(stubbed\): .*bogus"):
            through_class(gateway, 1, bogus=4)

        with pytest.raises(TypeError, match="stubbed"):
            through_class(gateway)

    assert seen == [((1,), {}), ((1,), {"currency": "EUR"})]


def test_a_target_with_no_signature_is_not_checked() -> None:
    import types

    class Opaque:
        # inspect.signature() gives up on this, as it does on many
        # C-implemented callables

        @property
        def __signature__(self) -> Any:
            raise ValueError("no signature")

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return "real"

    module: Any = types.ModuleType("nosig")
    module.f = Opaque()

    unknown = binding(module, "f")
    unknown.on_call.returns("stub")

    with unknown:
        assert module.f(1, bogus=2) == "stub"


def test_the_check_applies_per_phase() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("stub")
    charge.on_call.then(after=1)  # phase 1 passes through

    with charge:
        gateway: Any = Gateway()
        with pytest.raises(TypeError, match="stubbed"):
            gateway.charge(1, bogus=4)

        # the rejected call was not counted against the phase

        assert gateway.charge(1) == "stub"

        # phase 1 runs the real callable, which raises its own TypeError

        with pytest.raises(TypeError) as excinfo:
            gateway.charge(1, bogus=4)

        assert "stubbed" not in str(excinfo.value)


def test_async_targets_are_checked_at_call_time() -> None:
    acharge = binding(Gateway, "acharge")
    acharge.on_call.returns("stub")

    # The check runs at call time, where a coroutine function reports a
    # bad call shape too; the stub itself arrives on await.

    async def stubbed() -> Any:
        return await make().acharge(1)

    with acharge:
        assert asyncio.run(stubbed()) == "stub"

        with pytest.raises(TypeError, match="stubbed"):
            make().acharge(1, bogus=4)

    # and the coroutine function itself is unharmed afterwards

    async def run() -> Any:
        return await make().acharge(1)

    assert asyncio.run(run()) == {"id": "ch_1"}


def test_the_check_runs_whether_or_not_a_timeline_is_recording() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("stub")

    with charge, timeline() as tape:
        with pytest.raises(TypeError, match="stubbed"):
            make().charge(bogus=4)

    # a rejected call never reached the recording, as with mock's autospec

    assert tape.all == []

    quiet = binding(Gateway, "charge", when=False)
    quiet.on_call.returns("stub")

    with quiet:
        with pytest.raises(TypeError, match="stubbed"):
            make().charge(bogus=4)


def test_discover_and_groups_pass_strict_through() -> None:
    group = discover(Gateway, "charge", strict=False)
    group.charge.on_call.returns("stub")

    with group:
        assert make().charge(bogus=4) == "stub"

    checked = discover(Gateway, "charge")
    checked.charge.on_call.returns("stub")

    with checked:
        with pytest.raises(TypeError, match="stubbed"):
            make().charge(bogus=4)
