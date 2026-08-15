"""Tests that the scoping patterns documented in docs/unit-testing.md work.

The pytest fixtures and the unittest.TestCase here are the real thing, so
these tests exercise the documented patterns under the framework itself
rather than simulating them. The autouse leak-check fixture doubles as the
proof that every pattern removes its patch: it fails any test in this
module that finishes with Gateway.charge still wrapped.
"""

import unittest
from collections.abc import Generator
from typing import Any

import pytest
import wrapt

from wrapture import AlreadyAppliedError, Binding, binding


class Gateway:
    def charge(self, amount: int, currency: str = "USD") -> dict[str, Any]:
        return {"id": f"ch_{amount}", "amount": amount}


@pytest.fixture(autouse=True)
def no_leaked_patch() -> Generator[None, None, None]:
    # The leak-check recipe from docs/unit-testing.md, applied to every test in
    # this module. Tears down after each test's own fixtures, so it also
    # proves the stub_charge fixture really removed its patch.

    yield
    attr = vars(Gateway)["charge"]
    assert not issubclass(type(attr), wrapt.FunctionWrapper), (
        "a test left Gateway.charge patched"
    )


# ---------------------------------------------------------------------------
# pytest fixture scoping
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_charge() -> Generator[Binding, None, None]:
    with binding(Gateway, "charge").on_call.returns({"id": "stub"}) as bnd:
        yield bnd


def test_fixture_applies_the_stub(stub_charge: Binding) -> None:
    assert stub_charge.active
    assert Gateway().charge(1) == {"id": "stub"}


def test_fixture_can_be_reconfigured_in_flight(stub_charge: Binding) -> None:
    stub_charge.on_call.raises(TimeoutError("down"))
    with pytest.raises(TimeoutError):
        Gateway().charge(1)

    stub_charge.on_call.returns({"id": "retry"})
    assert Gateway().charge(1) == {"id": "retry"}


# ---------------------------------------------------------------------------
# unittest scoping with addCleanup
# ---------------------------------------------------------------------------


class UnittestCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.charge = binding(Gateway, "charge").on_call.returns({"id": "stub"}).apply()
        self.addCleanup(self.charge.remove)

    def test_stub_is_applied(self) -> None:
        self.assertEqual(Gateway().charge(1), {"id": "stub"})

    def test_cleanup_tolerates_early_removal(self) -> None:
        # remove() is idempotent, so a test removing the binding itself
        # does not break the registered cleanup.

        self.charge.remove()
        self.assertEqual(Gateway().charge(1), {"id": "ch_1", "amount": 1})


# ---------------------------------------------------------------------------
# shared declarations
# ---------------------------------------------------------------------------

shared_charge = binding(Gateway, "charge")


def test_shared_declaration_first_use() -> None:
    shared_charge.on_call.returns({"id": "first"})
    with shared_charge:
        assert Gateway().charge(1) == {"id": "first"}


def test_shared_declaration_reused_and_behaviour_persists() -> None:
    # The trap documented in docs/unit-testing.md: behaviour configured by an
    # earlier test is still there when a later test applies the same
    # binding, until passes_through() resets it.

    with shared_charge:
        assert Gateway().charge(1) == {"id": "first"}

    shared_charge.on_call.passes_through()
    with shared_charge:
        assert Gateway().charge(1) == {"id": "ch_1", "amount": 1}


# ---------------------------------------------------------------------------
# one lifecycle owner per binding
# ---------------------------------------------------------------------------


def test_entering_an_already_applied_binding_raises() -> None:
    bnd = binding(Gateway, "charge").apply()
    try:
        with pytest.raises(AlreadyAppliedError):
            with bnd:
                pass
    finally:
        bnd.remove()


# ---------------------------------------------------------------------------
# the leak check itself
# ---------------------------------------------------------------------------


def test_leak_check_detects_a_patched_target() -> None:
    bnd = binding(Gateway, "charge").apply()
    try:
        assert issubclass(type(vars(Gateway)["charge"]), wrapt.FunctionWrapper)
    finally:
        bnd.remove()
    assert not issubclass(type(vars(Gateway)["charge"]), wrapt.FunctionWrapper)
