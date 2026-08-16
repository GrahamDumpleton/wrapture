"""Tests for the event record and argument normalization.

Nothing here records through a binding: events are constructed directly,
which is the contract for this layer. Wiring events into the wrappers is
tested where that wiring lives.
"""

import gc
import weakref
from typing import Any

from wrapt import MISSING

from wrapture import Event
from wrapture.events import _signature, _signature_cache, normalized_arguments


def charge(amount: int, currency: str = "USD", *, retries: int = 3) -> None:
    pass


# ---------------------------------------------------------------------------
# the record itself
# ---------------------------------------------------------------------------


def test_missing_is_reexported_from_wrapture() -> None:
    # Docs teach `event.result is MISSING`; the sentinel is reachable
    # without knowing it comes from wrapt.

    import wrapture

    assert wrapture.MISSING is MISSING


def test_call_event_defaults() -> None:
    event = Event("call", "Gateway.charge")

    assert event.kind == "call"
    assert event.path == "Gateway.charge"
    assert event.result is MISSING
    assert event.exception is None
    assert event.value is MISSING
    assert event.previous is MISSING
    assert event.parent is None
    assert event.children == []
    assert event.depth == 0


def test_events_compare_by_identity() -> None:
    first = Event("call", "Gateway.charge")
    second = Event("call", "Gateway.charge")

    assert first == first
    assert first != second


def test_recorded_none_is_distinguishable_from_no_result() -> None:
    unfinished = Event("call", "Gateway.charge")
    returned_none = Event("call", "Gateway.charge", result=None)

    assert unfinished.result is MISSING
    assert returned_none.result is None


def test_parent_and_children_link_without_repr_recursion() -> None:
    parent = Event("call", "outer", seq=1, depth=0)
    child = Event("call", "inner", seq=2, depth=1, parent=parent)
    parent.children.append(child)

    assert child.parent is parent
    assert parent.children == [child]

    # The cyclic links are excluded from repr(), so neither direction
    # recurses.

    assert "inner" in repr(child)
    assert "outer" in repr(parent)


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------


def test_call_str_uses_normalized_arguments() -> None:
    event = Event(
        "call",
        "Gateway.charge",
        args=(500,),
        kwargs={},
        arguments={"amount": 500, "currency": "USD", "retries": 3},
    )

    assert str(event) == "Gateway.charge(amount=500, currency='USD', retries=3)"


def test_str_prefers_the_label_over_the_path() -> None:
    event = Event(
        "call",
        "myapp.orders:Gateway.charge",
        label="Gateway.charge",
        arguments={"amount": 500},
    )

    assert str(event) == "Gateway.charge(amount=500)"


def test_call_str_falls_back_to_raw_call_shape() -> None:
    event = Event("call", "Gateway.charge", args=(500,), kwargs={"currency": "AUD"})

    assert str(event) == "Gateway.charge(500, currency='AUD')"


def test_get_str_shows_value_read() -> None:
    assert str(Event("get", "Model.author", result="graham")) == (
        "get Model.author -> 'graham'"
    )
    assert str(Event("get", "Model.author")) == "get Model.author"


def test_set_str_shows_value_written() -> None:
    assert str(Event("set", "Model.author", value="graham")) == (
        "set Model.author = 'graham'"
    )


def test_delete_str() -> None:
    assert str(Event("delete", "Model.author")) == "delete Model.author"


# ---------------------------------------------------------------------------
# argument normalization
# ---------------------------------------------------------------------------


def test_call_forms_normalize_identically() -> None:
    expected = {"amount": 500, "currency": "USD", "retries": 3}

    assert normalized_arguments(charge, (500,), {}) == expected
    assert normalized_arguments(charge, (500, "USD"), {}) == expected
    assert normalized_arguments(charge, (500,), {"currency": "USD"}) == expected
    assert normalized_arguments(charge, (), {"amount": 500}) == expected


def test_normalization_returns_none_without_a_signature() -> None:
    # inspect.signature raises ValueError for type(), so this exercises
    # the no-signature fallback.

    assert normalized_arguments(type, (42,), {}) is None


def test_normalization_returns_none_when_arguments_do_not_fit() -> None:
    assert normalized_arguments(charge, (1, 2, 3, 4), {}) is None
    assert normalized_arguments(charge, (), {"unknown": 1}) is None


def test_unhashable_callable_falls_back_to_uncached_lookup() -> None:
    class Unhashable:
        __hash__ = None  # type: ignore[assignment]

        def __call__(self, amount: int) -> None:
            pass

    assert normalized_arguments(Unhashable(), (500,), {}) == {"amount": 500}


# ---------------------------------------------------------------------------
# the signature cache
# ---------------------------------------------------------------------------


def test_signature_lookup_is_cached() -> None:
    assert _signature(charge) is _signature(charge)


def test_failed_lookup_is_cached_as_none() -> None:
    _signature(type)

    assert _signature_cache[type] is None


def test_cache_does_not_keep_the_function_alive() -> None:
    def local(a: int) -> None:
        pass

    _signature(local)
    alive: weakref.ref[Any] = weakref.ref(local)
    del local

    gc.collect()
    assert alive() is None
