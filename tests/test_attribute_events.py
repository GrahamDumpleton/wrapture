"""Tests that attribute access through a binding records events.

The attribute-mode counterpart of test_recording.py: what get, set and
delete events contain, how they interact with behaviour and capture, and
that both modes land on one tape with the same nesting rules.
"""

from typing import Any

import pytest
from wrapt import MISSING

from wrapture import annotate, binding, redact, timeline


class Model:
    author = "unknown"

    def __init__(self) -> None:
        self.status = "draft"


class Basket:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.priced = 0

    def price(self) -> int:
        self.priced += 1
        return len(self.items) * 10

    @property
    def total(self) -> int:
        return self.price()


# ---------------------------------------------------------------------------
# what the three kinds record
# ---------------------------------------------------------------------------


def test_a_read_records_a_get_event_with_the_value_as_result() -> None:
    author = binding(Model, "author")
    model = Model()

    with timeline(author) as tape:
        assert model.author == "unknown"

    (event,) = tape.all

    assert event.kind == "get"
    assert event.path == f"{Model.__module__}:Model.author"
    assert event.label == "Model.author"
    assert event.instance is model
    assert event.result == "unknown"
    assert event.value is MISSING
    assert event.exception is None


def test_a_write_records_a_set_event_with_value_and_previous() -> None:
    status = binding(Model, "status", missing_ok=True)
    model = Model()

    with timeline(status) as tape:
        model.status = "published"
        model.status = "archived"

    first, second = tape.all

    assert first.kind == "set"
    assert first.value == "published"
    assert first.previous == "draft"
    assert second.value == "archived"
    assert second.previous == "published"


def test_a_write_with_no_prior_instance_value_records_no_previous() -> None:
    author = binding(Model, "author")
    model = Model()

    with timeline(author) as tape:
        model.author = "graham"

    (event,) = tape.all

    # The class default is not cheaply readable without running user
    # code in the general case, so previous stays unrecorded.

    assert event.value == "graham"
    assert event.previous is MISSING


def test_a_delete_records_a_delete_event() -> None:
    status = binding(Model, "status", missing_ok=True)
    model = Model()

    with timeline(status) as tape:
        del model.status

    (event,) = tape.all

    assert event.kind == "delete"
    assert event.previous == "draft"


def test_a_failing_read_records_the_attribute_error() -> None:
    missing = binding(Model, "missing", missing_ok=True)
    model = Model()

    with timeline(missing) as tape:
        with pytest.raises(AttributeError):
            _ = model.missing  # type: ignore[attr-defined]

    (event,) = tape.all

    assert event.kind == "get"
    assert isinstance(event.exception, AttributeError)
    assert event.result is MISSING


# ---------------------------------------------------------------------------
# behaviour and recording together
# ---------------------------------------------------------------------------


def test_a_stubbed_read_records_the_injected_value() -> None:
    author = binding(Model, "author").on_get.returns("stubbed")

    with timeline(author):
        assert Model().author == "stubbed"

        event = author.events.first
        assert event.result == "stubbed"
        assert event.injected


def test_a_rejected_write_records_the_injected_error() -> None:
    author = binding(Model, "author").on_set.rejects()
    model = Model()

    with timeline(author):
        with pytest.raises(AttributeError):
            model.author = "graham"

        event = author.events.first
        assert event.kind == "set"
        assert event.value == "graham"
        assert isinstance(event.exception, AttributeError)
        assert event.injected


def test_a_transformed_write_records_the_callers_value() -> None:
    status = binding(Model, "status", missing_ok=True)
    status.on_set.transforms(str.upper)
    model = Model()

    with timeline(status):
        model.status = "published"

        assert model.status == "PUBLISHED"
        assert status.events.of_kind("set").first.value == "published"


# ---------------------------------------------------------------------------
# filters against real attribute events
# ---------------------------------------------------------------------------


def test_filters_bridge_and_narrow_across_kinds() -> None:
    status = binding(Model, "status", missing_ok=True)
    model = Model()

    with timeline(status):
        _ = model.status
        model.status = "published"
        del model.status

        events = status.events
        events.assert_times(3)

        assert events.of_kind("get").count == 1
        assert events.of_kind("set", "delete").count == 2
        assert events.returning("draft").assert_once().first.kind == "get"
        assert events.with_value("published").assert_once().first.kind == "set"
        assert events.with_args(amount=1).count == 0


# ---------------------------------------------------------------------------
# nesting across the two modes
# ---------------------------------------------------------------------------


def test_calls_triggered_by_a_property_read_nest_under_the_get_event() -> None:
    total = binding(Basket, "total")
    price = binding(Basket, "price")

    with timeline(total, price) as tape:
        basket = Basket()
        basket.items.append("widget")
        assert basket.total == 10

    outer, inner = tape.all

    assert outer.kind == "get"
    assert outer.label == "Basket.total"
    assert inner.kind == "call"
    assert inner.label == "Basket.price"
    assert inner.parent is outer
    assert outer.children == [inner]
    assert outer.result == 10


# ---------------------------------------------------------------------------
# capture and annotation on attribute events
# ---------------------------------------------------------------------------


def test_set_values_capture_at_the_argument_level() -> None:
    status = binding(Model, "status", missing_ok=True, capture="summary")
    model = Model()
    tags = ["a", "b"]

    with timeline(status):
        model.status = tags  # type: ignore[assignment]

        tags.append("c")
        assert status.events.first.value == "<list ['a', 'b']>"


def test_redact_applies_to_writes_by_attribute_name() -> None:
    secret = binding(Model, "status", missing_ok=True, capture_args=redact("status"))
    model = Model()

    with timeline(secret):
        model.status = "hunter2"

        event = secret.events.first
        assert event.value == "<redacted>"
        assert event.previous == "<redacted>"

    assert model.status == "hunter2"


def test_annotate_lands_on_the_in_flight_attribute_event() -> None:
    def around(read: Any, instance: Any) -> Any:
        annotate(route="decorated read")
        return read()

    author = binding(Model, "author").on_get.decorates(around)

    with timeline(author):
        assert Model().author == "unknown"

        assert author.events.first.data == {"route": "decorated read"}


# ---------------------------------------------------------------------------
# ways attribute access records nothing
# ---------------------------------------------------------------------------


def test_suspended_attribute_binding_records_nothing_but_counts() -> None:
    author = binding(Model, "author")
    model = Model()

    with timeline(author):
        author.suspend()
        _ = model.author
        author.resume()
        _ = model.author

        author.events.assert_once()
        assert author.suspended_calls == 1


def test_no_timeline_means_no_recording_but_behaviour_still_applies() -> None:
    author = binding(Model, "author").on_get.returns("stubbed")

    with author:
        assert Model().author == "stubbed"


def test_class_level_access_is_not_recorded() -> None:
    author = binding(Model, "author")

    with timeline(author):
        # Class access returns the descriptor rather than firing it, the
        # documented limitation of attribute mode.

        _ = Model.author
        author.events.assert_never()
