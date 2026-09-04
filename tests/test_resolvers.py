"""Tests for per-operation declarations: binding(..., label=fn),
category=fn and data=fn, the resolvers.

A label, a category and seed data are declared on a binding once,
but a seam that fronts several kinds of operation can declare each
as a callable with the when= predicate's signature instead,
consulted per operation to decide that value for the one event. The
three are consulted together, after when= has accepted the operation
and before its event is built, only while something is listening,
under the recorder guard; a resolved label names the event, not the
binding, which is then identified by its path.
"""

import asyncio
import io
from collections.abc import Generator
from typing import Any

import pytest

import wrapture
from wrapture import (
    Printer,
    binding,
    current_event,
    find_binding,
    observed,
    timeline,
)


class Client:
    def __init__(self, service: str) -> None:
        self.service = service

    def dispatch(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": operation}


class Model:
    status = "draft"


class AsyncClient:
    def __init__(self, service: str) -> None:
        self.service = service

    async def dispatch(self, operation: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"ok": operation}


class Feed:
    def __init__(self, service: str) -> None:
        self.service = service

    def stream(self, operation: str, count: int) -> Generator[int, None, None]:
        yield from range(count)


KINDS = {"s3": "external", "dynamodb": "datastore", "sqs": "messaging"}


def kind_of(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    return KINDS.get(instance.service)


def name_of(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    return f"{instance.service}/{args[0]}"


def tags_of(
    instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    return {"service": instance.service, "operation": args[0]}


def workload() -> None:
    Client("s3").dispatch("GetObject", {"Bucket": "b"})
    Client("sqs").dispatch("SendMessage", {"QueueUrl": "q"})
    Client("iam").dispatch("ListUsers", {})


# ---------------------------------------------------------------------------
# each resolver decides per operation
# ---------------------------------------------------------------------------


def test_a_category_resolver_decides_each_events_category() -> None:
    dispatch = binding(Client, "dispatch", category=kind_of)

    with timeline(dispatch) as tape:
        workload()

    assert [event.category for event in tape.all] == ["external", "messaging", None]
    assert [event.path for event in tape.all] == [dispatch.path] * 3


def test_a_label_resolver_names_each_event_and_the_path_stays() -> None:
    dispatch = binding(Client, "dispatch", label=name_of)

    with timeline(dispatch) as tape:
        workload()

    assert [event.label for event in tape.all] == [
        "s3/GetObject",
        "sqs/SendMessage",
        "iam/ListUsers",
    ]
    assert all(event.path == dispatch.path for event in tape.all)


def test_a_resolved_label_is_what_renderers_show() -> None:
    output = io.StringIO()
    dispatch = binding(Client, "dispatch", label=name_of, capture="none")
    printer = Printer(output, timing=False)

    wrapture.add_sink(printer)
    try:
        with dispatch:
            Client("s3").dispatch("GetObject", {})
    finally:
        wrapture.remove_sink(printer)

    assert output.getvalue().splitlines()[-1].startswith("s3/GetObject")


def test_a_data_resolver_seeds_each_event_and_annotate_still_wins() -> None:
    dispatch = binding(Client, "dispatch", data=tags_of)

    def notes(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(operation="overridden", status=200)
        return wrapped(*args, **kwargs)

    dispatch.on_call.decorates(notes)

    with timeline(dispatch) as tape:
        Client("s3").dispatch("GetObject", {})

    assert tape.all[0].data == {
        "service": "s3",
        "operation": "overridden",
        "status": 200,
    }


def test_the_three_resolve_together_from_the_same_call_shape() -> None:
    seen: list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]] = []

    def noting(name: str, answer: Any) -> Any:
        def resolve(
            instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> Any:
            seen.append((name, instance, args, kwargs))
            return answer

        return resolve

    dispatch = binding(
        Client,
        "dispatch",
        label=noting("label", "named"),
        category=noting("category", "external"),
        data=noting("data", {"tag": 1}),
    )

    with timeline(dispatch) as tape:
        client = Client("s3")
        client.dispatch("GetObject", params={"Bucket": "b"})

    assert [name for name, *_ in seen] == ["label", "category", "data"]
    assert all(
        (instance, args, kwargs)
        == (client, ("GetObject",), {"params": {"Bucket": "b"}})
        for _, instance, args, kwargs in seen
    )

    (event,) = tape.all
    assert (event.label, event.category, event.data) == (
        "named",
        "external",
        {"tag": 1},
    )


def test_none_from_a_resolver_means_the_unlabelled_untagged_default() -> None:
    nothing = lambda instance, args, kwargs: None  # noqa: E731

    dispatch = binding(
        Client, "dispatch", label=nothing, category=nothing, data=nothing
    )

    with timeline(dispatch) as tape:
        Client("s3").dispatch("GetObject", {})

    (event,) = tape.all
    assert event.label is None
    assert event.category is None
    assert event.data == {}


# ---------------------------------------------------------------------------
# when they run
# ---------------------------------------------------------------------------


def test_resolvers_are_not_consulted_for_an_operation_when_declines() -> None:
    consulted: list[str] = []

    def kind(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        consulted.append(instance.service)
        return "external"

    dispatch = binding(
        Client,
        "dispatch",
        category=kind,
        when=lambda instance, args, kwargs: instance.service != "iam",
    )

    with timeline(dispatch) as tape:
        workload()

    assert consulted == ["s3", "sqs"]
    assert len(tape.all) == 2
    assert dispatch.filtered_calls == 1


def test_resolvers_are_not_consulted_when_nothing_listens() -> None:
    consulted: list[str] = []

    def name(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        consulted.append(args[0])
        return str(args[0])

    with binding(Client, "dispatch", label=name):
        Client("s3").dispatch("GetObject", {})

    assert consulted == []


def test_resolver_consultations_are_never_themselves_recorded() -> None:
    # The resolver reaches into observed code: what it calls passes
    # through unrecorded, so the tape holds the operation alone.

    @observed
    def lookup(service: str) -> str:
        return str(KINDS.get(service, "external"))

    dispatch = binding(
        Client,
        "dispatch",
        category=lambda instance, args, kwargs: lookup(instance.service),
    )

    with timeline(dispatch) as tape:
        Client("s3").dispatch("GetObject", {})

    (event,) = tape.all
    assert event.binding is dispatch
    assert event.category == "external"


def test_a_raising_resolver_propagates_to_the_caller() -> None:
    def broken(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        raise RuntimeError("no kind for you")

    with binding(Client, "dispatch", category=broken), timeline() as tape:
        with pytest.raises(RuntimeError, match="no kind for you"):
            Client("s3").dispatch("GetObject", {})

    assert tape.all == []


def test_a_behaviour_handler_sees_the_resolved_identity() -> None:
    seen: list[tuple[str | None, str | None]] = []

    def handler(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        event = current_event(binding=dispatch)
        seen.append((event.label, event.category))
        assert (
            current_event(category="messaging") if instance.service == "sqs" else True
        )
        return wrapped(*args, **kwargs)

    dispatch = binding(Client, "dispatch", label=name_of, category=kind_of)
    dispatch.on_call.decorates(handler)

    with timeline(dispatch):
        workload()

    assert seen == [
        ("s3/GetObject", "external"),
        ("sqs/SendMessage", "messaging"),
        ("iam/ListUsers", None),
    ]


def test_a_coroutine_call_resolves_at_the_call_and_records_the_await() -> None:
    dispatch = binding(
        AsyncClient, "dispatch", label=name_of, category=kind_of, data=tags_of
    )

    with timeline(dispatch) as tape:
        assert asyncio.run(AsyncClient("sqs").dispatch("SendMessage")) == {
            "ok": "SendMessage"
        }

    (event,) = tape.all
    assert event.label == "sqs/SendMessage"
    assert event.category == "messaging"
    assert event.data == {"service": "sqs", "operation": "SendMessage"}
    assert event.result == {"ok": "SendMessage"}


def test_a_generator_call_resolves_once_at_construction() -> None:
    consulted: list[str] = []

    def name(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        consulted.append(args[0])
        return f"{instance.service}/{args[0]}"

    stream = binding(Feed, "stream", label=name, category=kind_of)

    with timeline(stream) as tape:
        assert list(Feed("s3").stream("ListObjects", 3)) == [0, 1, 2]

    (event,) = tape.all
    assert event.label == "s3/ListObjects"
    assert event.category == "external"
    assert event.items == 3
    assert consulted == ["ListObjects"]


def test_attribute_accesses_hand_resolvers_the_call_shape() -> None:
    seen: list[tuple[Any, ...]] = []

    def name(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        seen.append(args)
        return "set" if args else "get"

    status = binding(Model, "status", mode="attribute", label=name)

    with timeline(status) as tape:
        model = Model()
        model.status = "final"
        _ = model.status

    assert seen == [("final",), ()]
    assert [event.label for event in tape.all] == ["set", "get"]


# ---------------------------------------------------------------------------
# what an answer may be
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("option", ["label", "category"])
def test_a_name_resolver_must_answer_a_string_or_none(option: str) -> None:
    def three(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
        return 3

    if option == "label":
        dispatch = binding(Client, "dispatch", label=three)
    else:
        dispatch = binding(Client, "dispatch", category=three)

    with dispatch, timeline():
        with pytest.raises(TypeError, match=f"a {option} resolver must return"):
            Client("s3").dispatch("GetObject", {})


def test_a_data_resolver_answer_is_checked_as_a_declaration_is() -> None:
    dispatch = binding(
        Client, "dispatch", data=lambda i, a, k: {"params": {"nested": True}}
    )

    with dispatch, timeline():
        with pytest.raises(TypeError, match=r"data\['params'\]"):
            Client("s3").dispatch("GetObject", {})


def test_a_category_resolver_may_answer_a_word_outside_the_vocabulary() -> None:
    # The vocabulary is what consumers understand, not what a resolver
    # is held to: the word is carried as given.

    dispatch = binding(Client, "dispatch", category=lambda i, a, k: "storage")

    with timeline(dispatch) as tape:
        Client("s3").dispatch("GetObject", {})

    assert tape.all[0].category == "storage"


@pytest.mark.parametrize("value", [3, "", b"bytes"])
def test_a_static_label_must_be_a_non_empty_string(value: Any) -> None:
    with pytest.raises(TypeError, match="label must be a non-empty string"):
        binding(Client, "dispatch", label=value)


def test_a_request_mode_binding_takes_static_declarations_only() -> None:
    def app(environ: Any, start_response: Any) -> list[bytes]:
        return [b""]

    apps = {"main": app}

    def nothing(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        return None

    with pytest.raises(TypeError, match="a wsgi binding takes a static"):
        binding(apps, item="main", mode="wsgi", label=nothing)

    with pytest.raises(TypeError, match="a wsgi binding takes a static"):
        binding(apps, item="main", mode="wsgi", category=nothing)

    with pytest.raises(TypeError, match="a wsgi binding takes a static"):
        binding(apps, item="main", mode="wsgi", data=nothing)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_a_binding_with_a_label_resolver_goes_by_its_path() -> None:
    with binding(Client, "dispatch", label=name_of) as dispatch:
        assert find_binding(Client, "dispatch") is dispatch
        assert find_binding(label=dispatch.path) is dispatch
        assert repr(dispatch) == f"<Binding {dispatch.path!r} callable active>"

    assert dispatch.label is name_of


def test_the_export_names_and_kinds_each_span_from_the_resolved_answers() -> None:
    # The export reads the event, so a resolved label is the span name
    # for a category with no naming rule of its own, and a resolved
    # category sets the span kind, both varying call by call from one
    # binding.

    pytest.importorskip("opentelemetry")

    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import SpanKind

    from wrapture.otel import OpenTelemetrySink

    exporter = InMemorySpanExporter()
    sink = OpenTelemetrySink(processor=SimpleSpanProcessor(exporter))
    dispatch = binding(Client, "dispatch", label=name_of, category=kind_of)

    wrapture.add_sink(sink)
    try:
        with timeline(dispatch):
            Client("dynamodb").dispatch("PutItem", {})
            Client("sqs").dispatch("SendMessage", {})
        wrapture.flush_sinks()
    finally:
        wrapture.remove_sink(sink)

    spans = {span.name: span for span in exporter.get_finished_spans()}

    assert spans["dynamodb/PutItem"].kind is SpanKind.CLIENT
    assert spans["sqs/SendMessage"].kind is SpanKind.PRODUCER
    attributes = spans["dynamodb/PutItem"].attributes or {}
    assert attributes["wrapture.path"] == dispatch.path


def test_observed_takes_the_same_resolvers() -> None:

    def send(queue: str) -> str:
        return f"sent to {queue}"

    wrapped = observed(
        send,
        label=lambda instance, args, kwargs: f"send/{args[0]}",
        category=lambda instance, args, kwargs: "messaging",
        data=lambda instance, args, kwargs: {"destination": args[0]},
    )

    with timeline() as tape:
        wrapped("orders")

    (event,) = tape.all
    assert event.label == "send/orders"
    assert event.category == "messaging"
    assert event.data == {"destination": "orders"}


def test_observed_takes_static_seed_data() -> None:
    def send(queue: str) -> None:
        pass

    wrapped = observed(send, data={"system": "bus"})

    with timeline() as tape:
        wrapped("orders")

    assert tape.all[0].data == {"system": "bus"}


def test_observed_with_a_label_resolver_dedupes_by_path() -> None:
    def send(queue: str) -> None:
        pass

    wrapped = observed(send, label=lambda instance, args, kwargs: args[0])
    again = observed(wrapped)

    assert again is wrapped
    assert find_binding(label=wrapped.path) is wrapped
