"""Tests for per-call record predicates: binding(..., when=fn).

The predicate decides, per operation and before any event is
constructed, whether recording happens at all. It is consulted only
while something is listening, sees (instance, args, kwargs) with
attribute accesses mapped onto call shape, runs under the recorder
guard, and counts declined operations on filtered_calls. It gates
recording only: behaviour still applies to a filtered operation.
"""

from collections.abc import Generator
from typing import Any

import pytest

from wrapture import binding, timeline


class Gateway:
    def __init__(self, tenant: str = "acme") -> None:
        self.tenant = tenant

    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


class Audit:
    def note(self, entry: str) -> str:
        return f"noted:{entry}"


class Feed:
    def stream(self, count: int) -> Generator[int, None, None]:
        yield from range(count)


class Model:
    status = "draft"


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------


def test_when_records_only_operations_the_predicate_accepts() -> None:
    charge = binding(
        Gateway,
        "charge",
        when=lambda instance, args, kwargs: args[0] > 100,
    )

    with timeline(charge) as tape:
        gateway = Gateway()
        gateway.charge(50)
        gateway.charge(500)

    assert [event.arguments for event in tape.all] == [{"amount": 500}]
    assert charge.filtered_calls == 1


def test_the_predicate_sees_the_instance() -> None:
    charge = binding(
        Gateway,
        "charge",
        when=lambda instance, args, kwargs: instance.tenant == "acme",
    )

    with timeline(charge) as tape:
        Gateway("acme").charge(1)
        Gateway("other").charge(2)

    assert [event.arguments for event in tape.all] == [{"amount": 1}]


def test_a_filtered_call_still_runs_behaviour() -> None:
    charge = binding(
        Gateway, "charge", when=lambda instance, args, kwargs: False
    ).on_call.returns("stubbed")

    with timeline(charge) as tape:
        assert Gateway().charge(500) == "stubbed"

    assert tape.all == []
    assert charge.filtered_calls == 1


def test_the_predicate_is_not_consulted_when_nothing_listens() -> None:
    consulted: list[bool] = []

    def note(instance: Any, args: Any, kwargs: Any) -> bool:
        consulted.append(True)
        return True

    charge = binding(Gateway, "charge", when=note)

    with charge:
        Gateway().charge(500)
        assert consulted == []
        assert charge.filtered_calls == 0

        with timeline():
            Gateway().charge(500)

    assert len(consulted) == 1


def test_a_raising_predicate_propagates_to_the_caller() -> None:
    def broken(instance: Any, args: Any, kwargs: Any) -> bool:
        raise RuntimeError("broken predicate")

    charge = binding(Gateway, "charge", when=broken)

    with timeline(charge) as tape:
        with pytest.raises(RuntimeError, match="broken predicate"):
            Gateway().charge(500)

    assert tape.all == []


def test_predicate_consultations_are_never_themselves_recorded() -> None:
    # The predicate runs under the recorder guard, so observed code it
    # calls to reach its answer stays off the tape.

    def asks_the_audit(instance: Any, args: Any, kwargs: Any) -> bool:
        return Audit().note("considering") is not None

    note = binding(Audit, "note")
    charge = binding(Gateway, "charge", when=asks_the_audit)

    with timeline(charge, note) as tape:
        Gateway().charge(500)

    assert [event.label for event in tape.all] == ["Gateway.charge"]


def test_a_generator_call_is_decided_once_at_construction() -> None:
    consulted: list[bool] = []

    def note(instance: Any, args: Any, kwargs: Any) -> bool:
        consulted.append(True)
        return True

    stream = binding(Feed, "stream", when=note)

    with timeline(stream) as tape:
        assert list(Feed().stream(3)) == [0, 1, 2]

    (event,) = tape.all
    assert event.items == 3
    assert len(consulted) == 1


# ---------------------------------------------------------------------------
# attribute accesses
# ---------------------------------------------------------------------------


def test_attribute_accesses_map_onto_call_shape() -> None:
    # A set passes the written value as the one positional argument; a
    # get passes empty args. Record gets, and only large writes.

    status = binding(
        Model,
        "status",
        when=lambda instance, args, kwargs: not args or len(args[0]) > 5,
    )

    with timeline(status) as tape:
        model = Model()
        model.status = "ok"
        model.status = "published"
        _ = model.status

    events = tape.all
    assert [event.kind for event in events] == ["set", "get"]
    assert events[0].value == "published"
    assert events[1].result == "published"
    assert status.filtered_calls == 1


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_a_non_callable_predicate_is_rejected_at_creation() -> None:
    with pytest.raises(ValueError, match="when must be a callable"):
        binding(Gateway, "charge", when=True)  # type: ignore[arg-type]
