"""Tests for binding lifecycle, modes, groups and deferred rejection."""

import gc
import weakref
from typing import Any

import pytest
import wrapt

from wrapture import (
    AlreadyAppliedError,
    Binding,
    DeferredTargetError,
    NotImplementedYetError,
    WrongModeError,
    binding,
    bindings,
)


class Gateway:
    def charge(self, amount: int, currency: str = "USD") -> dict[str, Any]:
        return {"id": f"ch_{amount}", "amount": amount}

    def refund(self, charge_id: str) -> dict[str, Any]:
        return {"refunded": charge_id}


class Ledger:
    rate = 0.05  # data attribute: detected as attribute mode

    def record(self, entry: str) -> str:
        return f"led-{entry}"


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_binding_does_not_apply() -> None:
    bnd = binding(Gateway, "charge")
    assert not bnd.applied
    assert not bnd.active
    assert "unapplied" in repr(bnd)


def test_apply_returns_self_so_it_chains() -> None:
    bnd = binding(Gateway, "charge").apply()
    try:
        assert bnd.active
    finally:
        bnd.remove()


def test_context_manager_applies_and_removes() -> None:
    bnd = binding(Gateway, "charge")
    with bnd:
        assert bnd.active
    assert not bnd.active


def test_remove_is_idempotent() -> None:
    bnd = binding(Gateway, "charge").apply()
    bnd.remove()
    bnd.remove()
    assert not bnd.active


def test_reusable_across_apply_remove_cycles() -> None:
    bnd = binding(Gateway, "charge")
    for _ in range(3):
        bnd.apply()
        assert bnd.active
        bnd.remove()
    assert not bnd.active


def test_declared_at_class_scope_has_no_import_time_effect() -> None:
    class Holder:
        charge = binding(Gateway, "charge")

    assert not Holder.charge.applied


def test_mixing_lifecycle_styles_is_an_error() -> None:
    bnd = binding(Gateway, "charge").apply()
    try:
        with pytest.raises(AlreadyAppliedError):
            bnd.apply()
    finally:
        bnd.remove()


def test_displaced_state_is_reported_honestly() -> None:
    # Capture the original from the class __dict__ before patching:
    # getattr() on a patched class returns a fresh BoundFunctionWrapper,
    # and restoring that would reinstall a stale wrapper.

    original = vars(Ledger)["record"]
    bnd = binding(Ledger, "record").apply()
    try:
        Ledger.record = lambda self, entry: "hijacked"  # type: ignore[method-assign]
        assert bnd.applied  # we installed it
        assert not bnd.active  # but it is gone
        assert "displaced" in repr(bnd)
    finally:
        Ledger.record = original  # type: ignore[method-assign]
        bnd.remove()
    assert vars(Ledger)["record"] is original


def test_wrapper_is_the_wrapt_handle() -> None:
    bnd = binding(Gateway, "charge").apply()
    try:
        current = wrapt.resolve_path(bnd.target, bnd.name)[2]
        assert wrapt.is_wrapped_by(current, bnd.wrapper)
    finally:
        bnd.remove()


def test_wrapper_is_a_plain_wrapt_function_wrapper() -> None:
    bnd = binding(Gateway, "charge").apply()
    try:
        assert type(bnd.wrapper) is wrapt.FunctionWrapper
    finally:
        bnd.remove()


def test_unknown_name_fails_at_creation() -> None:
    with pytest.raises(AttributeError):
        binding(Ledger, "does_not_exist")


# ---------------------------------------------------------------------------
# suspend / resume
# ---------------------------------------------------------------------------


def test_suspended_binding_is_inert() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge").on_call.returns({"id": "STUB"}).apply()
    try:
        assert gw.charge(1) == {"id": "STUB"}
        bnd.suspend()
        assert gw.charge(1) == {"id": "ch_1", "amount": 1}
        bnd.resume()
        assert gw.charge(1) == {"id": "STUB"}
    finally:
        bnd.remove()


def test_suspended_stays_applied() -> None:
    bnd = binding(Gateway, "charge").apply()
    try:
        bnd.suspend()
        assert bnd.applied
        assert bnd.active  # orthogonal to suspension
        assert bnd.suspended
        assert "suspended" in repr(bnd)
    finally:
        bnd.remove()


def test_apply_can_start_suspended() -> None:
    gw = Gateway()
    bnd = (
        binding(Gateway, "charge").on_call.returns({"id": "STUB"}).apply(suspended=True)
    )
    try:
        assert bnd.applied and bnd.suspended
        assert gw.charge(1)["id"] == "ch_1"  # inert until resumed
        bnd.resume()
        assert gw.charge(1)["id"] == "STUB"
    finally:
        bnd.remove()


def test_suspended_calls_are_counted() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge").apply()
    try:
        gw.charge(1)
        bnd.suspend()
        gw.charge(2)
        gw.charge(3)
        bnd.resume()
        gw.charge(4)
        assert bnd.suspended_calls == 2
    finally:
        bnd.remove()


def test_reconfigure_while_suspended() -> None:
    gw = Gateway()
    bnd = binding(Gateway, "charge").on_call.returns({"id": "A"}).apply()
    try:
        assert gw.charge(1)["id"] == "A"
        bnd.suspend()
        bnd.on_call.returns({"id": "B"})
        bnd.resume()
        assert gw.charge(1)["id"] == "B"
    finally:
        bnd.remove()


def test_remove_clears_suspension() -> None:
    bnd = binding(Gateway, "charge").apply(suspended=True)
    bnd.remove()
    assert not bnd.suspended
    bnd.apply()
    try:
        assert not bnd.suspended
    finally:
        bnd.remove()


# ---------------------------------------------------------------------------
# deferred patching is rejected
# ---------------------------------------------------------------------------


def test_deferred_target_syntax_is_rejected() -> None:
    with pytest.raises(DeferredTargetError, match="deferred patching"):
        binding("mymodule?", "handler")

    with pytest.raises(DeferredTargetError):
        bindings(bad=("mymodule?", "handler"))


def test_only_a_trailing_question_mark_on_a_string_target_is_rejected() -> None:
    # not a string target: nothing to reject

    binding(Gateway, "charge")

    # `?` in the name is not special; it simply will not resolve, and mode
    # detection surfaces that at creation

    with pytest.raises(AttributeError):
        binding("os.path", "join?")

    with pytest.raises(DeferredTargetError):
        binding("os.path?", "join")


def test_deferred_target_error_is_a_value_error() -> None:
    assert issubclass(DeferredTargetError, ValueError)


# ---------------------------------------------------------------------------
# modes and behaviour namespaces
# ---------------------------------------------------------------------------


def test_mode_is_detected_from_the_target() -> None:
    assert binding(Gateway, "charge").mode == "callable"
    assert binding(Ledger, "rate").mode == "attribute"
    assert binding("os.path", "join").mode == "callable"

    class WithProperty:
        @property
        def value(self) -> int:
            return 1

    assert binding(WithProperty, "value").mode == "attribute"


def test_mode_can_be_overridden() -> None:
    assert binding(Ledger, "rate", mode="callable").mode == "callable"
    with pytest.raises(ValueError):
        binding(Gateway, "charge", mode="nonsense")


def test_absent_attribute_needs_missing_ok() -> None:
    with pytest.raises(AttributeError):
        binding(Gateway, "not_on_the_class")

    bnd = binding(Gateway, "not_on_the_class", missing_ok=True)
    assert bnd.mode == "attribute"


def test_namespaces_gate_on_mode() -> None:
    call = binding(Gateway, "charge")
    attr = binding(Ledger, "rate")

    assert type(call.on_call).__name__ == "CallBehaviour"
    assert type(attr.on_get).__name__ == "GetBehaviour"
    assert type(attr.on_set).__name__ == "SetBehaviour"
    assert type(attr.on_delete).__name__ == "DeleteBehaviour"

    with pytest.raises(WrongModeError, match="on_get is not available"):
        _ = call.on_get
    with pytest.raises(WrongModeError, match="on_call is not available"):
        _ = attr.on_call


def test_wrong_mode_error_keeps_hasattr_working() -> None:
    call = binding(Gateway, "charge")
    attr = binding(Ledger, "rate")

    assert hasattr(call, "on_call")
    assert not hasattr(call, "on_get")
    assert hasattr(attr, "on_get")
    assert not hasattr(attr, "on_call")
    assert issubclass(WrongModeError, AttributeError)


def test_attribute_mode_apply_is_stubbed_loudly() -> None:
    with pytest.raises(NotImplementedYetError):
        binding(Ledger, "rate").apply()


def test_repr_names_the_mode() -> None:
    assert "callable" in repr(binding(Gateway, "charge"))
    assert "attribute" in repr(binding(Ledger, "rate"))


# ---------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------


def test_group_attribute_and_item_access() -> None:
    group = bindings(charge=(Gateway, "charge"), record=(Ledger, "record"))
    assert isinstance(group.charge, Binding)
    assert group["record"] is group.record
    assert len(group) == 2
    assert list(group) == [group.charge, group.record]
    with pytest.raises(AttributeError):
        _ = group.not_there


def test_group_applies_and_removes_as_a_unit() -> None:
    group = bindings(charge=(Gateway, "charge"), record=(Ledger, "record"))
    with group:
        assert group.active
    assert not group.charge.active
    assert not group.record.active


def test_group_rolls_back_on_partial_failure() -> None:
    group = bindings(good=(Ledger, "record"), bad=(Ledger, "rate"))
    with pytest.raises(NotImplementedYetError):
        group.apply()
    assert not group["good"].active


def test_group_suspend_and_resume() -> None:
    group = bindings(charge=(Gateway, "charge"), record=(Ledger, "record"))
    with group:
        group.suspend()
        assert group.suspended
        group.resume()
        assert not group["charge"].suspended


# ---------------------------------------------------------------------------
# lifetime and garbage collection
# ---------------------------------------------------------------------------


def test_binding_is_collected_once_removed() -> None:
    def make() -> weakref.ref[Binding]:
        bnd = binding(Gateway, "charge").apply()
        bnd.remove()
        return weakref.ref(bnd)

    ref = make()
    gc.collect()
    assert ref() is None


def test_binding_stays_alive_while_its_wrapper_is_applied() -> None:
    # Not a leak: the target class holds the wrapper, which reaches the
    # binding through the wrapper closure.

    def make() -> weakref.ref[Binding]:
        bnd = binding(Gateway, "charge").apply()
        return weakref.ref(bnd)

    ref = make()
    gc.collect()
    alive = ref()
    assert alive is not None
    alive.remove()
    del alive
    gc.collect()
    assert ref() is None


def test_apply_remove_churn_does_not_leak() -> None:
    gc.collect()
    before = len(gc.get_objects())
    for _ in range(2000):
        bnd = binding(Gateway, "charge").apply()
        bnd.remove()
        del bnd
    gc.collect()
    after = len(gc.get_objects())
    assert after - before < 100  # allow noise, catch per-cycle growth
