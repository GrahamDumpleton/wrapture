"""Tests for filter_requests() and the tree= flag.

filter_requests() declares a request predicate as tables of request
fields to glob patterns, accepted by when= on the request modes and
refused elsewhere. tree= extends a when= decline to everything
beneath the declined operation: nested bindings, attribute accesses,
blocks and log captures record nothing for its extent, a generator's
iteration and a coroutine's await included, and the suppression
follows the context into threads. An observe entry's requests table
is the filter spelt as TOML, with tree=True implied.
"""

import asyncio
import logging
import sys
import textwrap
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

import wrapture
from wrapture import (
    ASGIMiddleware,
    Config,
    ConfigError,
    ObserveEntry,
    RequestFilter,
    WSGIMiddleware,
    binding,
    capture_logs,
    detach,
    filter_requests,
    load_config,
    observed,
    propagate,
    timeline,
)

# ---------------------------------------------------------------------------
# fixtures: a small world of observable code
# ---------------------------------------------------------------------------


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


class Ledger:
    def record(self, entry: str) -> str:
        return f"noted:{entry}"


class Model:
    status = "draft"


class Service:
    def __init__(self, tenant: str = "acme") -> None:
        self.tenant = tenant
        self.gateway = Gateway()
        self.ledger = Ledger()

    def place(self, amount: int) -> str:
        return self.gateway.charge(amount)

    def stream(self, count: int) -> Generator[str, None, None]:
        for n in range(count):
            yield self.gateway.charge(n)

    async def fetch(self, amount: int) -> str:
        await asyncio.sleep(0)
        return self.gateway.charge(amount)

    def in_thread(self, amount: int) -> None:
        work = propagate(self.gateway.charge)
        thread = threading.Thread(target=work, args=(amount,))
        thread.start()
        thread.join()

    def detached(self, amount: int) -> None:
        work = detach(self.gateway.charge)
        thread = threading.Thread(target=work, args=(amount,))
        thread.start()
        thread.join()


gateway = Gateway()


def application(environ: dict[str, Any], start_response: Any) -> Any:
    """A streaming app: an observed call before the response and one
    more per body chunk."""

    gateway.charge(1)
    start_response("200 OK", [("Content-Type", "text/plain")])

    def body() -> Generator[bytes, None, None]:
        gateway.charge(2)
        yield b"hello "
        gateway.charge(3)
        yield b"world"

    return body()


def list_application(environ: dict[str, Any], start_response: Any) -> Any:
    gateway.charge(1)
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"hello world"]


async def asgi_application(scope: dict[str, Any], receive: Any, send: Any) -> None:
    gateway.charge(1)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"hello ", "more_body": True})
    gateway.charge(2)
    await send({"type": "http.response.body", "body": b"world", "more_body": False})


def _environ(**overrides: Any) -> dict[str, Any]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": "GET",
        "SCRIPT_NAME": "",
        "PATH_INFO": "/orders/42",
        "QUERY_STRING": "",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.url_scheme": "http",
        "REMOTE_ADDR": "127.0.0.1",
    }
    environ.update(overrides)
    return environ


def _scope(**overrides: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/orders/42",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 52000),
    }
    scope.update(overrides)
    return scope


def _serve(app: Any, environ: dict[str, Any]) -> tuple[bytes, str]:
    """Play the WSGI server: call, drain the body, close it."""

    status: list[str] = []

    def start_response(line: str, headers: Any, exc_info: Any = None) -> Any:
        status.append(line)
        return lambda data: None

    iterable = app(environ, start_response)
    try:
        body = b"".join(iterable)
    finally:
        close = getattr(iterable, "close", None)
        if close is not None:
            close()

    return body, status[0]


async def _serve_asgi(app: Any, scope: dict[str, Any]) -> bytes:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


def _kinds(tape: Any) -> list[str]:
    return [event.kind for event in tape.all]


# ---------------------------------------------------------------------------
# filter_requests(): construction and matching
# ---------------------------------------------------------------------------


def _fields(**overrides: Any) -> dict[str, Any]:
    fields = {
        "method": "GET",
        "path": "/orders/42",
        "scheme": "http",
        "protocol": "HTTP/1.1",
        "remote": "127.0.0.1",
    }
    fields.update(overrides)
    return fields


def test_ignore_declines_a_request_matching_any_pattern_of_any_field() -> None:
    ignoring = filter_requests(
        ignore={"path": ["/health", "/static/*"], "method": "HEAD"}
    )

    assert ignoring.matches(_fields()) is True
    assert ignoring.matches(_fields(path="/health")) is False
    assert ignoring.matches(_fields(path="/static/logo.png")) is False
    assert ignoring.matches(_fields(method="HEAD")) is False


def test_accept_requires_every_listed_field_to_match() -> None:
    accepting = filter_requests(accept={"method": ["GET", "POST"], "scheme": "https"})

    assert accepting.matches(_fields(scheme="https")) is True
    assert accepting.matches(_fields(method="POST", scheme="https")) is True
    assert accepting.matches(_fields(scheme="http")) is False
    assert accepting.matches(_fields(method="DELETE", scheme="https")) is False


def test_ignore_wins_where_both_tables_apply() -> None:
    both = filter_requests(accept={"path": "/api/*"}, ignore={"path": "/api/health"})

    assert both.matches(_fields(path="/api/orders")) is True
    assert both.matches(_fields(path="/api/health")) is False
    assert both.matches(_fields(path="/orders")) is False


def test_a_list_of_plain_strings_reads_as_a_set_of_exact_values() -> None:
    accepting = filter_requests(accept={"remote": ["10.0.0.1", "10.0.0.2"]})

    assert accepting.matches(_fields(remote="10.0.0.2")) is True
    assert accepting.matches(_fields(remote="10.0.0.20")) is False


def test_methods_compare_case_insensitively_and_nothing_else_does() -> None:
    accepting = filter_requests(accept={"method": "get", "scheme": "HTTPS"})

    assert accepting.matches(_fields(method="GET", scheme="HTTPS")) is True
    assert accepting.matches(_fields(method="GET", scheme="https")) is False


def test_a_non_string_value_matches_no_pattern() -> None:
    # Nothing is rendered to test it: a missing or oddly typed field
    # passes ignore and fails accept.

    assert filter_requests(ignore={"remote": "*"}).matches({"path": "/x"}) is True
    assert filter_requests(accept={"remote": "*"}).matches({"path": "/x"}) is False
    assert filter_requests(accept={"remote": "*"}).matches({"remote": None}) is False


def test_the_filter_reports_its_tables() -> None:
    both = filter_requests(accept={"method": "get"}, ignore={"path": "/health"})

    assert isinstance(both, RequestFilter)
    assert both.accept == {"method": ("GET",)}
    assert both.ignore == {"path": ("/health",)}
    assert repr(both) == (
        "filter_requests(accept={'method': ('GET',)}, ignore={'path': ('/health',)})"
    )
    assert not callable(both)


def test_at_least_one_table_is_required() -> None:
    with pytest.raises(ValueError, match="accept=, ignore=, or both"):
        filter_requests()


def test_an_empty_table_is_refused_rather_than_never_acting() -> None:
    with pytest.raises(ValueError, match="ignore is empty"):
        filter_requests(ignore={})


def test_a_field_that_is_not_a_request_field_is_refused() -> None:
    with pytest.raises(ValueError, match="'query', which is not a request field"):
        filter_requests(ignore={"query": "*token*"})


def test_patterns_must_be_strings() -> None:
    with pytest.raises(TypeError, match="glob pattern or a non-empty list"):
        filter_requests(ignore={"path": [1, 2]})  # type: ignore[list-item]

    with pytest.raises(TypeError, match="glob pattern or a non-empty list"):
        filter_requests(ignore={"path": []})


def test_a_table_must_be_a_mapping() -> None:
    with pytest.raises(TypeError, match="accept must be a mapping"):
        filter_requests(accept=["/health"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the request modes: the filter as when=, with and without tree=
# ---------------------------------------------------------------------------


def test_a_wsgi_binding_takes_the_filter_and_tree_drops_the_whole_request() -> None:
    app = binding(
        __name__,
        "application",
        mode="wsgi",
        when=filter_requests(ignore={"path": ["/health", "/static/*"]}),
        tree=True,
    )
    charge = binding(Gateway, "charge")

    with app, charge, timeline() as tape:
        body, status = _serve(
            sys.modules[__name__].application, _environ(PATH_INFO="/health")
        )
        assert body == b"hello world"
        assert status == "200 OK"

        assert tape.all == []

        _serve(sys.modules[__name__].application, _environ())

    # The recorded request carries its three calls, the ignored one
    # nothing, not even the calls made while streaming the body.

    assert _kinds(tape) == ["request", "call", "call", "call"]
    assert app.filtered_calls == 1
    assert charge.filtered_calls == 3


def test_without_tree_a_declined_request_leaves_its_calls_as_roots() -> None:
    app = binding(
        __name__,
        "application",
        mode="wsgi",
        when=filter_requests(ignore={"path": "/health"}),
    )
    charge = binding(Gateway, "charge")

    with app, charge, timeline() as tape:
        _serve(sys.modules[__name__].application, _environ(PATH_INFO="/health"))

    assert _kinds(tape) == ["call", "call", "call"]
    assert all(event.parent_id is None for event in tape.all)
    assert charge.filtered_calls == 0


def test_the_standalone_wsgi_middleware_silences_the_streaming_body() -> None:
    wrapped = WSGIMiddleware(
        application, when=filter_requests(ignore={"path": "/health"}), tree=True
    )
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        body, _ = _serve(wrapped, _environ(PATH_INFO="/health"))
        assert body == b"hello world"

    assert tape.all == []
    assert charge.filtered_calls == 3


def test_a_materialised_body_is_returned_untouched_when_silenced() -> None:
    wrapped = WSGIMiddleware(
        list_application, when=filter_requests(ignore={"path": "/health"}), tree=True
    )
    charge = binding(Gateway, "charge")

    def start_response(line: str, headers: Any, exc_info: Any = None) -> Any:
        return None

    with charge, timeline() as tape:
        iterable = wrapped(_environ(PATH_INFO="/health"), start_response)

    assert iterable == [b"hello world"]
    assert tape.all == []


def test_the_servers_file_wrapper_is_returned_untouched_when_silenced() -> None:
    class FileWrapper:
        def __init__(self, filelike: Any) -> None:
            self.filelike = filelike

        def __iter__(self) -> Any:
            return iter([b"file body"])

    def file_application(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [])
        return environ["wsgi.file_wrapper"](object())

    wrapped = WSGIMiddleware(
        file_application, when=filter_requests(ignore={"path": "/static/*"}), tree=True
    )

    with timeline():
        iterable = wrapped(
            _environ(
                **{"PATH_INFO": "/static/a.png", "wsgi.file_wrapper": FileWrapper}
            ),
            lambda *a: None,
        )

    assert isinstance(iterable, FileWrapper)


def test_a_silenced_body_is_closed_exactly_once() -> None:
    closes: list[int] = []

    class Body:
        def __iter__(self) -> Any:
            return iter([b"x"])

        def close(self) -> None:
            closes.append(1)

    def closing_application(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [])
        return Body()

    wrapped = WSGIMiddleware(
        closing_application, when=filter_requests(ignore={"path": "/health"}), tree=True
    )

    with timeline():
        iterable = wrapped(_environ(PATH_INFO="/health"), lambda *a: None)
        assert list(iterable) == [b"x"]
        iterable.close()
        iterable.close()

    assert closes == [1]


def test_an_asgi_binding_takes_the_filter_and_tree_drops_the_whole_request() -> None:
    app = binding(
        __name__,
        "asgi_application",
        mode="asgi",
        when=filter_requests(ignore={"path": "/health"}),
        tree=True,
    )
    charge = binding(Gateway, "charge")

    with app, charge, timeline() as tape:
        module = sys.modules[__name__]
        body = asyncio.run(_serve_asgi(module.asgi_application, _scope(path="/health")))
        assert body == b"hello world"
        assert tape.all == []

        asyncio.run(_serve_asgi(module.asgi_application, _scope()))

    assert _kinds(tape) == ["request", "call", "call"]
    assert app.filtered_calls == 1
    assert charge.filtered_calls == 2


def test_the_standalone_asgi_middleware_silences_beneath_a_declined_request() -> None:
    wrapped = ASGIMiddleware(
        asgi_application, when=filter_requests(accept={"method": "POST"}), tree=True
    )
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        asyncio.run(_serve_asgi(wrapped, _scope()))
        asyncio.run(_serve_asgi(wrapped, _scope(method="POST")))

    assert _kinds(tape) == ["request", "call", "call"]
    assert tape.all[0].data["method"] == "POST"


def test_a_behaviour_only_request_binding_with_tree_silences_the_application() -> None:
    app = binding(__name__, "application", mode="wsgi", when=False, tree=True)
    charge = binding(Gateway, "charge")

    with app, charge, timeline() as tape:
        _serve(sys.modules[__name__].application, _environ())

    assert tape.all == []
    assert app.filtered_calls == 0
    assert charge.filtered_calls == 3


def test_a_request_beneath_a_silenced_operation_is_declined_unconsulted() -> None:
    consulted: list[str] = []

    def wanted(environ: dict[str, Any]) -> bool:
        consulted.append(environ["PATH_INFO"])
        return True

    wrapped = WSGIMiddleware(application, when=wanted)
    outer = observed(
        lambda: _serve(wrapped, _environ()), when=lambda *a: False, tree=True
    )

    with timeline() as tape:
        outer()

    assert tape.all == []
    assert consulted == []


# ---------------------------------------------------------------------------
# refusals: where the filter and the flag do not apply
# ---------------------------------------------------------------------------


def test_the_filter_is_refused_on_a_call_binding() -> None:
    with pytest.raises(ValueError, match="wsgi or asgi binding only"):
        binding(Gateway, "charge", when=filter_requests(ignore={"path": "/x"}))


def test_the_filter_is_refused_on_an_attribute_binding() -> None:
    with pytest.raises(ValueError, match="wsgi or asgi binding only"):
        binding(Model, "status", when=filter_requests(ignore={"path": "/x"}))


def test_the_filter_is_refused_by_observed() -> None:
    with pytest.raises(ValueError, match="wsgi or asgi binding only"):
        observed(  # type: ignore[call-overload]
            Gateway().charge, when=filter_requests(ignore={"path": "/x"})
        )


def test_tree_needs_a_when_to_act_on() -> None:
    with pytest.raises(ValueError, match="needs a when= predicate"):
        binding(Gateway, "charge", tree=True)

    with pytest.raises(ValueError, match="needs a when= predicate"):
        observed(Gateway().charge, tree=True)

    with pytest.raises(ValueError, match="needs a when= predicate"):
        WSGIMiddleware(application, tree=True)

    with pytest.raises(ValueError, match="needs a when= predicate"):
        ASGIMiddleware(asgi_application, tree=True)


def test_tree_must_be_a_boolean() -> None:
    with pytest.raises(TypeError, match="tree must be True or False"):
        binding(Gateway, "charge", when=False, tree="yes")  # type: ignore[arg-type]


def test_tree_is_refused_on_a_value_binding() -> None:
    with pytest.raises(ValueError, match="tree= do not apply"):
        binding(Model, attr="status", tree=True)


# ---------------------------------------------------------------------------
# tree= on call bindings: the extent of the silence
# ---------------------------------------------------------------------------


def test_a_tree_decline_silences_the_calls_beneath_it() -> None:
    place = binding(
        Service,
        "place",
        when=lambda instance, args, kwargs: instance.tenant == "acme",
        tree=True,
    )
    charge = binding(Gateway, "charge")

    with place, charge, timeline() as tape:
        assert Service("other").place(5) == "ch_5"
        assert Service("acme").place(7) == "ch_7"

    assert _kinds(tape) == ["call", "call"]
    assert tape.all[1].parent_id == tape.all[0].seq
    assert place.filtered_calls == 1
    assert charge.filtered_calls == 1


def test_without_tree_the_calls_beneath_a_decline_record_as_roots() -> None:
    place = binding(Service, "place", when=lambda *a: False)
    charge = binding(Gateway, "charge")

    with place, charge, timeline() as tape:
        Service().place(5)

    assert _kinds(tape) == ["call"]
    assert tape.all[0].parent_id is None
    assert charge.filtered_calls == 0


def test_when_false_with_tree_silences_a_subtree() -> None:
    place = binding(Service, "place", when=False, tree=True)
    charge = binding(Gateway, "charge")

    with place, charge, timeline() as tape:
        Service().place(5)
        gateway.charge(9)

    assert [event.args for event in tape.all] == [(9,)]
    assert place.filtered_calls == 0
    assert charge.filtered_calls == 1


def test_the_silence_covers_a_generators_iteration() -> None:
    stream = binding(Service, "stream", when=lambda *a: False, tree=True)
    charge = binding(Gateway, "charge")

    with stream, charge, timeline() as tape:
        assert list(Service().stream(3)) == ["ch_0", "ch_1", "ch_2"]

    assert tape.all == []
    assert charge.filtered_calls == 3


def test_the_silence_covers_a_coroutines_await() -> None:
    fetch = binding(Service, "fetch", when=lambda *a: False, tree=True)
    charge = binding(Gateway, "charge")

    with fetch, charge, timeline() as tape:
        assert asyncio.run(Service().fetch(4)) == "ch_4"

    assert tape.all == []
    assert charge.filtered_calls == 1


def test_the_silence_covers_attribute_accesses_blocks_and_logs() -> None:
    log = logging.getLogger("filters.inner")
    log.setLevel(logging.DEBUG)

    def work() -> None:
        model = Model()
        model.status = "published"
        with wrapture.block("render"):
            log.warning("inside")

    silenced = observed(work, when=lambda *a: False, tree=True)
    status = binding(Model, "status")
    logs = capture_logs("filters.*")

    with status, logs, timeline() as tape:
        silenced()

    assert tape.all == []
    assert status.filtered_calls == 1


def test_an_attribute_binding_with_tree_silences_what_the_access_triggers() -> None:
    class Lazy:
        @property
        def total(self) -> str:
            return gateway.charge(1)

    total = binding(Lazy, "total", when=lambda *a: False, tree=True)
    charge = binding(Gateway, "charge")

    with total, charge, timeline() as tape:
        assert Lazy().total == "ch_1"

    assert tape.all == []
    assert total.filtered_calls == 1
    assert charge.filtered_calls == 1


def test_the_silence_follows_the_context_into_a_propagated_thread() -> None:
    in_thread = binding(Service, "in_thread", when=lambda *a: False, tree=True)
    charge = binding(Gateway, "charge")

    with in_thread, charge, timeline() as tape:
        Service().in_thread(3)

    assert tape.all == []
    assert charge.filtered_calls == 1
    assert charge.missed_calls == 0


def test_a_detached_thread_beneath_the_silence_stays_silenced() -> None:
    detached = binding(Service, "detached", when=lambda *a: False, tree=True)
    charge = binding(Gateway, "charge")

    with detached, charge, timeline() as tape:
        Service().detached(3)

    assert tape.all == []
    assert charge.filtered_calls == 1


def test_a_timeline_opened_beneath_the_silence_hears_nothing_either() -> None:
    # The silence is a property of the code path, not of who listens:
    # a scope opened inside the extent records nothing from it, which
    # also keeps the outer tape, which hears every nested scope's
    # events, honest about the decline.

    charge = binding(Gateway, "charge")
    inner_tape: list[Any] = []

    def work() -> None:
        with timeline(charge) as tape:
            gateway.charge(1)
        inner_tape.append(tape)

    silenced = observed(work, when=lambda *a: False, tree=True)

    with timeline() as tape:
        silenced()

    assert tape.all == []
    assert inner_tape[0].all == []
    assert charge.filtered_calls == 1


def test_observed_with_tree_silences_beneath_a_decline() -> None:
    charge = binding(Gateway, "charge")

    @observed(when=lambda instance, args, kwargs: args[0] > 10, tree=True)
    def order(amount: int) -> str:
        return gateway.charge(amount)

    with charge, timeline() as tape:
        order(5)
        order(50)

    assert [event.args for event in tape.all] == [(50,), (50,)]
    assert order.filtered_calls == 1
    assert charge.filtered_calls == 1


def test_bound_passes_tree_through() -> None:
    charge = binding(Gateway, "charge")

    @wrapture.bound(Service, "place", when=lambda *a: False, tree=True)
    def run(place: Any) -> None:
        with charge, timeline() as tape:
            Service().place(1)
        assert tape.all == []
        assert place.filtered_calls == 1

    run()


# ---------------------------------------------------------------------------
# config: the requests table on an observe entry
# ---------------------------------------------------------------------------


def test_the_requests_table_needs_a_request_mode() -> None:
    with pytest.raises(ConfigError, match="requests applies to a wsgi or asgi entry"):
        ObserveEntry(
            target=__name__, name="application", requests={"ignore": {"path": "/x"}}
        )


def test_the_requests_table_is_validated_at_load() -> None:
    with pytest.raises(ConfigError, match="requests: unknown keys \\['deny'\\]"):
        ObserveEntry(
            target=__name__, name="application", mode="wsgi", requests={"deny": {}}
        )

    with pytest.raises(ConfigError, match="requests: ignore names 'query'"):
        ObserveEntry(
            target=__name__,
            name="application",
            mode="wsgi",
            requests={"ignore": {"query": "*"}},
        )

    with pytest.raises(ConfigError, match="requests must be a table"):
        ObserveEntry(target=__name__, name="application", mode="wsgi", requests="/x")  # type: ignore[arg-type]


def test_the_requests_table_filters_whole_request_trees() -> None:
    entry = ObserveEntry(
        target=__name__,
        name="application",
        mode="wsgi",
        requests={"ignore": {"path": ["/health", "/static/*"]}},
    )
    charge = binding(Gateway, "charge")

    applied = Config(observe=[entry]).apply()
    try:
        with charge, timeline() as tape:
            _serve(sys.modules[__name__].application, _environ(PATH_INFO="/health"))
            _serve(sys.modules[__name__].application, _environ())
    finally:
        applied.revert()

    assert _kinds(tape) == ["request", "call", "call", "call"]
    assert charge.filtered_calls == 3


def test_the_loader_accepts_the_inline_and_long_forms(tmp_path: Path) -> None:
    inline = tmp_path / "inline.toml"
    inline.write_text(
        textwrap.dedent(
            f"""
            [[observe]]
            target = "{__name__}"
            name = "application"
            mode = "wsgi"
            requests = {{ ignore = {{ path = "/health" }} }}
            """
        )
    )

    long = tmp_path / "long.toml"
    long.write_text(
        textwrap.dedent(
            f"""
            [[observe]]
            target = "{__name__}"
            name = "application"
            mode = "wsgi"

            [observe.requests]
            ignore.path = "/health"
            accept.method = "GET"
            """
        )
    )

    assert load_config(inline).observe[0].requests == {"ignore": {"path": "/health"}}
    assert load_config(long).observe[0].requests == {
        "ignore": {"path": "/health"},
        "accept": {"method": "GET"},
    }


def test_the_loader_rejects_a_bad_requests_table(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[observe]]
            target = "{__name__}"
            name = "application"
            mode = "wsgi"
            requests = {{ ignore = {{ path = [] }} }}
            """
        )
    )

    with pytest.raises(ConfigError, match="requests: ignore\\['path'\\]"):
        load_config(source)
