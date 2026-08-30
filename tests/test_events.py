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


def _both_ways(func: Any, *shapes: tuple[tuple[Any, ...], dict[str, Any]]) -> None:
    # The parameter table must agree with Signature.bind exactly, on
    # the values, on which shapes are rejected, and on the order of
    # the names, since the dict's order is what str(event) and the
    # serialised record show.

    info = signature_info(func)
    assert info.signature is not None

    for args, kwargs in shapes:
        slow = normalized_arguments(info.signature, args, kwargs)
        fast = normalized_arguments(info.signature, args, kwargs, info.table)

        assert fast == slow, (args, kwargs)
        if slow is not None and fast is not None:
            assert list(fast.items()) == list(slow.items()), (args, kwargs)


def test_the_parameter_table_binds_like_signature_bind() -> None:
    def simple(a: int, b: int = 2, c: int = 3) -> None:
        pass

    assert signature_info(simple).table is not None

    _both_ways(
        simple,
        ((1,), {}),
        ((1, 20), {}),
        ((1, 20, 30), {}),
        ((1,), {"c": 30}),
        ((1,), {"c": 30, "b": 20}),
        ((), {"a": 1}),
        ((), {"c": 30, "a": 1}),
        ((1, 20), {"c": 30}),
        # Rejected shapes: too many positionals, a keyword for a
        # parameter already given positionally, an unknown keyword,
        # a required parameter left unfilled.
        ((1, 2, 3, 4), {}),
        ((1,), {"a": 5}),
        ((1,), {"d": 5}),
        ((), {"b": 2}),
        ((), {}),
    )


def test_the_parameter_table_respects_positional_only() -> None:
    def constrained(a: int, /, b: int, c: int = 3) -> None:
        pass

    table = signature_info(constrained).table
    assert table is not None and table.positional_only == 1

    _both_ways(
        constrained,
        ((1, 2), {}),
        ((1,), {"b": 2}),
        ((1,), {"b": 2, "c": 4}),
        # A positional-only parameter cannot be passed by keyword.
        ((), {"a": 1, "b": 2}),
        ((1,), {"a": 1, "b": 2}),
    )


def test_signatures_beyond_the_table_fall_back_to_bind() -> None:
    def variadic(a: int, *rest: Any) -> None:
        pass

    def keyword_only(a: int, *, flag: bool = False) -> None:
        pass

    def options(a: int, **extra: Any) -> None:
        pass

    for func in (variadic, keyword_only, options):
        assert signature_info(func).table is None

    _both_ways(variadic, ((1, 2, 3), {}), ((1,), {}))
    _both_ways(keyword_only, ((1,), {}), ((1,), {"flag": True}), ((1, True), {}))
    _both_ways(options, ((1,), {"x": 2}), ((1,), {}))


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
