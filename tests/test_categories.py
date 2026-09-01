"""Tests for event categories: binding(..., category="external") and
its siblings.

A category is a declaration about what kind of operation a binding's
events are, fixed at the binding (or observed() or block()), carried
on every event as a field of its own, validated against a small fixed
vocabulary, and selected on everywhere events are selected: the tape
queries, the in-flight handle, the sink filter and its config table,
and the OpenTelemetry export, where it decides the span kind and maps
the category's data-key contract onto semantic-convention attributes.
It is never rendered: the printed tree and the canonical fingerprint
are unchanged by it.
"""

import io
import textwrap
from pathlib import Path
from typing import Any

import pytest

import wrapture
from wrapture import (
    Config,
    ConfigError,
    Filter,
    JSONLines,
    ObserveEntry,
    Printer,
    binding,
    canonical,
    current_event,
    load_config,
    load_events,
    observed,
    timeline,
)
from wrapture.events import CATEGORIES, Event

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class Gateway:
    def charge(self, amount: int) -> str:
        wrapture.annotate(method="POST", url="https://pay.example/charge", status=201)
        return f"ch_{amount}"


class Store:
    def query(self, sql: str) -> list[int]:
        wrapture.annotate(system="sqlite", operation="SELECT", url="not-a-contract-key")
        return [1]


class Bus:
    def publish(self, topic: str) -> None:
        wrapture.annotate(system="rabbitmq", destination=topic)


class Service:
    def __init__(self) -> None:
        self.gateway = Gateway()

    def place(self, amount: int) -> str:
        return self.gateway.charge(amount)


def _handle_inside() -> Any:
    return current_event(category="external")


class Probe:
    def call(self) -> Any:
        return _handle_inside()


gateway = Gateway()


# ---------------------------------------------------------------------------
# declaration and the event field
# ---------------------------------------------------------------------------


def test_a_binding_declares_the_category_of_its_events() -> None:
    charge = binding(Gateway, "charge", category="external")

    with charge, timeline() as tape:
        Gateway().charge(5)

    (event,) = tape.all
    assert event.category == "external"
    assert charge.category == "external"


def test_an_uncategorised_event_carries_none() -> None:
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        Gateway().charge(5)

    assert tape.all[0].category is None
    assert charge.category is None


def test_the_vocabulary_is_fixed_and_validated() -> None:
    assert CATEGORIES == (
        "external",
        "database",
        "datastore",
        "messaging",
        "task",
        "template",
    )

    with pytest.raises(ValueError, match="category must be one of external"):
        binding(Gateway, "charge", category="http")

    with pytest.raises(ValueError, match="category must be one of external"):
        observed(gateway.charge, category="")

    with pytest.raises(ValueError, match="category must be one of external"):
        wrapture.block("x", category="sql")


def test_observed_bound_and_block_take_a_category() -> None:
    @observed(category="database")
    def query() -> None:
        return None

    with timeline() as tape:
        query()
        with wrapture.block("publish", category="messaging"):
            pass

    assert [event.category for event in tape.all] == ["database", "messaging"]
    assert query.category == "database"

    @wrapture.bound(Gateway, "charge", category="external")
    def run(charge: Any) -> None:
        assert charge.category == "external"

    run()


def test_category_is_refused_on_a_value_binding() -> None:
    class Model:
        status = "draft"

    with pytest.raises(ValueError, match="leaf= and category= do not apply"):
        binding(Model, attr="status", category="external")


def test_category_is_a_field_not_a_data_key() -> None:
    charge = binding(Gateway, "charge", category="external", data={"team": "pay"})

    with charge, timeline() as tape:
        Gateway().charge(5)

    (event,) = tape.all
    assert event.category == "external"
    assert "category" not in event.data
    assert event.data["team"] == "pay"


# ---------------------------------------------------------------------------
# selecting by category
# ---------------------------------------------------------------------------


def test_the_tape_selects_by_category() -> None:
    charge = binding(Gateway, "charge", category="external")
    query = binding(Store, "query", category="database")
    place = binding(Service, "place")

    with charge, query, place, timeline() as tape:
        Service().place(5)
        Store().query("select 1")

        # A binding's own log narrows by category like any other verb.

        assert charge.events.of_category("external").count == 1
        assert charge.events.of_category("database").count == 0
        assert place.events.of_category("external", "database").count == 0
        assert charge.events.of_category("external").label.endswith("[external]")

    # The tape selects across bindings.

    assert [e.binding for e in tape.where(category="external")] == [charge]
    assert tape.where(category="database").assert_once().first.binding is query
    assert tape.where(path=charge.path, category="external").count == 1
    assert tape.where(path=charge.path, category="database").count == 0


def test_current_event_aims_at_the_nearest_enclosing_category() -> None:
    charge = binding(Gateway, "charge", category="external")
    probe = binding(Probe, "call")

    seen: list[Any] = []

    def inner(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        seen.append(current_event(category="external"))
        seen.append(current_event(category="database"))
        return wrapped(*args, **kwargs)

    probe.on_call.decorates(inner)

    with charge, probe, timeline() as tape:
        Gateway().charge(5)

        def within() -> None:
            Probe().call()

        wrapped = observed(within, category="external")
        wrapped()

    external, missing = seen
    assert external
    assert external.category == "external"
    assert external.path == tape.all[-2].path
    assert not missing
    assert "category='database'" in repr(missing) or not missing


def make_collector() -> Any:
    return _Collect()


class _Collect(wrapture.Sink):
    def __init__(self) -> None:
        self.entered: list[Event] = []

    def on_enter(self, event: Event) -> None:
        self.entered.append(event)


def test_a_filter_sink_and_its_config_table_select_by_category(
    tmp_path: Path,
) -> None:
    accepted: list[Event] = []

    class Collect(wrapture.Sink):
        def on_enter(self, event: Event) -> None:
            accepted.append(event)

    charge = binding(Gateway, "charge", category="external")
    place = binding(Service, "place")
    sink = Filter(lambda event: event.category == "external", Collect())

    wrapture.add_sink(sink)
    try:
        with charge, place, timeline():
            Service().place(5)
    finally:
        wrapture.remove_sink(sink)

    assert [event.binding for event in accepted] == [charge]

    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[sink]]
            type = "{__name__}:make_collector"
            filter = {{ category = ["external", "database"] }}
            """
        )
    )
    config = load_config(source)
    assert isinstance(config.sink, Filter)
    predicate = config.sink._predicate

    assert predicate(Event(kind="call", path="x", category="external"))
    assert predicate(Event(kind="call", path="x", category="database"))
    assert not predicate(Event(kind="call", path="x", category="template"))
    assert not predicate(Event(kind="call", path="x"))


# ---------------------------------------------------------------------------
# storage and rendering
# ---------------------------------------------------------------------------


def test_the_json_lines_record_carries_the_category(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)
    charge = binding(Gateway, "charge", category="external")
    place = binding(Service, "place")

    wrapture.add_sink(sink)
    try:
        with charge, place, timeline():
            Service().place(5)
    finally:
        wrapture.remove_sink(sink)
        sink.close()

    outer, inner = load_events(trace)
    assert "category" not in outer
    assert inner["category"] == "external"


def test_the_renderers_do_not_show_the_category() -> None:
    stream = io.StringIO()
    printer = Printer(stream=stream)
    charge = binding(Gateway, "charge", category="external")

    wrapture.add_sink(printer)
    try:
        with charge, timeline() as tape:
            Gateway().charge(5)
    finally:
        wrapture.remove_sink(printer)

    assert "external" not in stream.getvalue()
    assert canonical(tape) == f"call {charge.path}"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_an_observe_entry_takes_a_category(tmp_path: Path) -> None:
    entry = ObserveEntry(
        target=f"{__name__}:Gateway", name="charge", category="external"
    )

    applied = Config(observe=[entry]).apply()
    try:
        with timeline() as tape:
            Gateway().charge(5)
    finally:
        applied.revert()

    assert tape.all[0].category == "external"

    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[observe]]
            target = "{__name__}:Gateway"
            name = "charge"
            category = "external"
            leaf = true
            """
        )
    )
    loaded = load_config(source).observe[0]
    assert loaded.category == "external"
    assert loaded.leaf is True


def test_the_loader_rejects_a_bad_category() -> None:
    with pytest.raises(ConfigError, match="category must be one of external"):
        ObserveEntry(target=f"{__name__}:Gateway", name="charge", category="http")

    with pytest.raises(ConfigError, match="category must be a string"):
        ObserveEntry(target=f"{__name__}:Gateway", name="charge", category=3)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the OpenTelemetry export
# ---------------------------------------------------------------------------


def _exported(*bindings_: Any, run: Any) -> list[Any]:
    pytest.importorskip("opentelemetry")

    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from wrapture.otel import OpenTelemetrySink

    exporter = InMemorySpanExporter()
    sink = OpenTelemetrySink(processor=SimpleSpanProcessor(exporter))

    wrapture.add_sink(sink)
    try:
        with timeline(*bindings_):
            run()
        wrapture.flush_sinks()
    finally:
        wrapture.remove_sink(sink)

    return list(exporter.get_finished_spans())


def test_the_export_sets_the_span_kind_by_category() -> None:
    from opentelemetry.trace import SpanKind

    charge = binding(Gateway, "charge", category="external")
    query = binding(Store, "query", category="database")
    publish = binding(Bus, "publish", category="messaging")
    place = binding(Service, "place")

    def run() -> None:
        Service().place(5)
        Store().query("select 1")
        Bus().publish("orders")

    spans = {
        span.name: span for span in _exported(charge, query, publish, place, run=run)
    }

    assert spans[charge.path].kind is SpanKind.CLIENT
    assert spans[query.path].kind is SpanKind.CLIENT
    assert spans[publish.path].kind is SpanKind.PRODUCER
    assert spans[place.path].kind is SpanKind.INTERNAL


def test_the_export_maps_the_categorys_contract_keys() -> None:
    from opentelemetry.trace import StatusCode

    charge = binding(Gateway, "charge", category="external")
    query = binding(Store, "query", category="database")
    publish = binding(Bus, "publish", category="messaging")

    def run() -> None:
        Gateway().charge(5)
        Store().query("select 1")
        Bus().publish("orders")

    spans = {span.name: span for span in _exported(charge, query, publish, run=run)}

    external = spans[charge.path].attributes
    assert external["wrapture.category"] == "external"
    assert external["http.request.method"] == "POST"
    assert external["url.full"] == "https://pay.example/charge"
    assert external["http.response.status_code"] == 201
    assert "wrapture.data.url" not in external
    assert spans[charge.path].status.status_code is StatusCode.UNSET

    # A key that belongs to another category's contract is ordinary
    # data on this one.

    database = spans[query.path].attributes
    assert database["wrapture.category"] == "database"
    assert database["db.system.name"] == "sqlite"
    assert database["db.operation.name"] == "SELECT"
    assert database["wrapture.data.url"] == "not-a-contract-key"

    messaging = spans[publish.path].attributes
    assert messaging["messaging.system"] == "rabbitmq"
    assert messaging["messaging.destination.name"] == "orders"


def test_an_external_failure_status_marks_the_span_in_error() -> None:
    from opentelemetry.trace import StatusCode

    class Client:
        def get(self) -> None:
            wrapture.annotate(status="503 Service Unavailable")

    get = binding(Client, "get", category="external")

    (span,) = _exported(get, run=lambda: Client().get())

    assert span.attributes["http.response.status_code"] == 503
    assert span.status.status_code is StatusCode.ERROR


def test_an_uncategorised_span_carries_no_category_attribute() -> None:
    place = binding(Service, "place")

    (span,) = _exported(place, run=lambda: Service().place(5))

    assert "wrapture.category" not in span.attributes
