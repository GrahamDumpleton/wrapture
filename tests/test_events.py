"""Tests for the event record and argument normalization.

Nothing here records through a binding: events are constructed directly,
which is the contract for this layer. Wiring events into the wrappers is
tested where that wiring lives.
"""

import inspect
from typing import Any
from unittest.mock import patch

import pytest
from wrapt import MISSING

from wrapture import Event
from wrapture.events import (
    SignatureInfo,
    _own_time,
    cached_signature_info,
    normalized_arguments,
    signature_info,
)


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
    assert event.parent_id is None
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


def test_events_link_to_their_parent_by_sequence_number() -> None:
    parent = Event("call", "outer", seq=1, depth=0)
    child = Event("call", "inner", seq=2, depth=1, parent_id=1)

    assert parent.parent_id is None
    assert child.parent_id == parent.seq

    # The link is a plain integer, not a reference: an event can be
    # serialised without dragging its tree along, and repr() has no
    # cycle to recurse through.

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
    signature = inspect.signature(charge)

    assert normalized_arguments(signature, (500,), {}) == expected
    assert normalized_arguments(signature, (500, "USD"), {}) == expected
    assert normalized_arguments(signature, (500,), {"currency": "USD"}) == expected
    assert normalized_arguments(signature, (), {"amount": 500}) == expected


def test_normalization_returns_none_without_a_signature() -> None:
    assert normalized_arguments(None, (42,), {}) is None


def test_normalization_returns_none_when_arguments_do_not_fit() -> None:
    signature = inspect.signature(charge)

    assert normalized_arguments(signature, (1, 2, 3, 4), {}) is None
    assert normalized_arguments(signature, (), {"unknown": 1}) is None


# ---------------------------------------------------------------------------
# signature resolution and the per-owner cache
# ---------------------------------------------------------------------------


def test_signature_info_resolves_var_keyword_name() -> None:
    def f(a: int, **options: Any) -> None:
        pass

    info = signature_info(f)

    assert info.signature == inspect.signature(f)
    assert info.var_keyword == "options"
    assert signature_info(charge).var_keyword is None


def test_signature_info_without_a_signature_is_empty() -> None:
    # inspect.signature raises ValueError for type(), so this exercises
    # the no-signature fallback.

    assert signature_info(type) == SignatureInfo(None, None)


def test_cached_signature_info_resolves_once_per_presented_form() -> None:
    # A method reaches the wrapper as a fresh bound method object on
    # every access, so the cache keys on the type wrapt presented rather
    # than the object, and inspect.signature runs once per form.

    class Thing:
        def method(self, a: int) -> None:
            pass

    cache: dict[type, SignatureInfo] = {}
    thing = Thing()

    first = cached_signature_info(cache, thing.method)
    with patch("wrapture.events.inspect.signature") as resolve:
        second = cached_signature_info(cache, Thing().method)

    assert resolve.call_count == 0
    assert second is first
    assert first.signature == inspect.signature(thing.method)
    assert list(cache) == [type(thing.method)]


def test_cached_signature_info_keeps_forms_apart() -> None:
    # The same underlying function presented bound and unbound has two
    # different signatures, one with self and one without.

    class Thing:
        def method(self, a: int) -> None:
            pass

    cache: dict[type, SignatureInfo] = {}

    bound = cached_signature_info(cache, Thing().method).signature
    plain = cached_signature_info(cache, Thing.method).signature

    assert bound is not None and list(bound.parameters) == ["a"]
    assert plain is not None and list(plain.parameters) == ["self", "a"]
    assert len(cache) == 2


def test_own_time_of_a_request_sums_both_application_phases() -> None:
    # A request's own time is the synchronous call plus the body it
    # produced, not the wall time that includes the server between chunks.

    request = Event(kind="request", path="app:wsgi_app", duration=1.0)
    request.body_duration = 0.2
    request.data["app_duration"] = 0.3

    assert _own_time(request) == pytest.approx(0.5)


def test_own_time_of_a_generator_is_its_body_time() -> None:
    call = Event(kind="call", path="mod:gen", duration=1.0)
    call.body_duration = 0.25

    assert _own_time(call) == 0.25
