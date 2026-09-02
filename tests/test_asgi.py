"""Tests for the ASGI middleware, mode="asgi" bindings and on_request.

The middleware is driven directly with hand-built scopes and canned
receive messages, playing the server's part and collecting everything
sent. The protocol obligations each get a test: message forwarding in
native types, the untouched fast path handing the application the
original channels, non-HTTP scopes passing through, completion via the
final body message, and disconnects surfacing through receive.
"""

import asyncio
import sys
from typing import Any

import pytest

import wrapture
from wrapture import (
    ASGIMiddleware,
    Config,
    ConfigError,
    ObserveEntry,
    WrongModeError,
    binding,
    filter_requests,
    redact,
    timeline,
)


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


gateway = Gateway()

seen_scope: list[dict[str, Any]] = []


async def application(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """A small streaming app: one observed call, then two body chunks."""

    seen_scope.append(scope)
    gateway.charge(42)

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", b"11")],
        }
    )
    await send({"type": "http.response.body", "body": b"hello ", "more_body": True})
    await send({"type": "http.response.body", "body": b"world", "more_body": False})


def _scope(**overrides: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
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


class _Server:
    """The server half: canned receive messages, collected sends."""

    def __init__(self, *messages: dict[str, Any]) -> None:
        self._messages = list(messages) or [
            {"type": "http.request", "body": b"", "more_body": False}
        ]
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        if self._messages:
            return self._messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    @property
    def status(self) -> Any:
        for message in self.sent:
            if message["type"] == "http.response.start":
                return message["status"]
        return None

    @property
    def headers(self) -> Any:
        for message in self.sent:
            if message["type"] == "http.response.start":
                return message["headers"]
        return None

    @property
    def body(self) -> bytes:
        return b"".join(
            message.get("body", b"")
            for message in self.sent
            if message["type"] == "http.response.body"
        )


def _serve(scope: dict[str, Any], server: _Server | None = None) -> _Server:
    # Drive whatever currently sits at this module's application
    # attribute, middleware included, the way a server would.

    server = server or _Server()
    asyncio.run(sys.modules[__name__].application(scope, server.receive, server.send))
    return server


# ---------------------------------------------------------------------------
# the middleware standalone
# ---------------------------------------------------------------------------


def test_request_records_one_event_with_the_http_details() -> None:
    wrapped = ASGIMiddleware(application)
    server = _Server()

    with timeline() as tape:
        asyncio.run(
            wrapped(_scope(query_string=b"expand=items"), server.receive, server.send)
        )

    assert server.body == b"hello world"
    assert server.status == 200

    request = tape.all[0]
    assert request.kind == "request"
    assert request.result == "200 OK"
    assert request.items == 2
    assert request.data["interface"] == "asgi"
    assert request.data["method"] == "GET"
    assert request.data["path"] == "/orders/42"
    assert request.data["query"] == "expand=items"
    assert request.data["scheme"] == "http"
    assert request.data["protocol"] == "HTTP/1.1"
    assert request.data["remote"] == "127.0.0.1"
    assert request.data["content_type"] == "text/plain"
    assert request.data["content_length"] == 11
    assert request.data["bytes"] == 11
    assert request.data["app_duration"] is not None
    assert "incomplete" not in request.data
    assert request.duration is not None
    assert request.body_duration is not None


def test_request_line_display() -> None:
    wrapped = ASGIMiddleware(application)
    server = _Server()

    with timeline() as tape:
        asyncio.run(
            wrapped(_scope(query_string=b"expand=items"), server.receive, server.send)
        )

    assert str(tape.all[0]) == (f"GET /orders/42?expand=items ({__name__}:application)")


def test_calls_nest_under_the_request() -> None:
    wrapped = ASGIMiddleware(application)
    server = _Server()
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        asyncio.run(wrapped(_scope(), server.receive, server.send))

    request, call = tape.all
    assert request.kind == "request"
    assert call.kind == "call"
    assert call.parent_id == request.seq


def test_work_done_while_streaming_nests_under_the_request() -> None:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        gateway.charge(7)
        await send(
            {"type": "http.response.body", "body": b"second", "more_body": False}
        )

    wrapped = ASGIMiddleware(app)
    server = _Server()
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        asyncio.run(wrapped(_scope(), server.receive, server.send))

    request, call = tape.all
    assert call.parent_id == request.seq


def test_concurrent_requests_do_not_cross_link() -> None:
    # Each request runs in its own task with its own context copy, so
    # the calls beneath two interleaved requests must link to their own
    # roots, never each other's.

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0)
        gateway.charge(7)
        await asyncio.sleep(0)
        await send({"type": "http.response.body", "body": b"done", "more_body": False})

    wrapped = ASGIMiddleware(app)
    charge = binding(Gateway, "charge")

    async def run_two() -> None:
        first, second = _Server(), _Server()
        await asyncio.gather(
            wrapped(_scope(path="/a"), first.receive, first.send),
            wrapped(_scope(path="/b"), second.receive, second.send),
        )

    with charge, timeline() as tape:
        asyncio.run(run_two())

    requests = [event for event in tape.all if event.kind == "request"]
    calls = [event for event in tape.all if event.kind == "call"]

    assert len(requests) == 2
    assert len(calls) == 2
    assert {call.parent_id for call in calls} == {req.seq for req in requests}


def test_not_recording_passes_the_original_channels() -> None:
    # The fast path hands the application exactly the channels the
    # server supplied, wrapping nothing.

    seen: list[Any] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append((receive, send))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    wrapped = ASGIMiddleware(app)
    server = _Server()

    asyncio.run(wrapped(_scope(), server.receive, server.send))

    assert seen[0] == (server.receive, server.send)


def test_non_http_scopes_pass_through_untouched() -> None:
    seen: list[Any] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append((scope["type"], receive, send))

    wrapped = ASGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        asyncio.run(wrapped({"type": "lifespan"}, server.receive, server.send))

    assert seen[0] == ("lifespan", server.receive, server.send)
    assert [event.kind for event in tape.all] == []


def test_app_error_before_the_response_starts_is_recorded_and_raised() -> None:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        raise RuntimeError("app exploded")

    wrapped = ASGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        with pytest.raises(RuntimeError, match="app exploded"):
            asyncio.run(wrapped(_scope(), server.receive, server.send))

    request = tape.all[0]
    assert isinstance(request.exception, RuntimeError)
    assert request.result is wrapture.MISSING
    assert request.duration is not None
    assert "incomplete" not in request.data


def test_error_while_streaming_is_recorded_with_the_incomplete_body() -> None:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {"type": "http.response.body", "body": b"partial", "more_body": True}
        )
        raise OSError("pipe burst")

    wrapped = ASGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        with pytest.raises(OSError, match="pipe burst"):
            asyncio.run(wrapped(_scope(), server.receive, server.send))

    request = tape.all[0]
    assert isinstance(request.exception, OSError)
    assert request.items == 1
    assert request.data["incomplete"] is True


def test_a_response_never_completed_is_marked_incomplete() -> None:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})

    wrapped = ASGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        asyncio.run(wrapped(_scope(), server.receive, server.send))

    request = tape.all[0]
    assert request.items == 1
    assert request.data["incomplete"] is True
    assert request.result == "200 OK"
    assert request.duration is not None


def test_a_disconnect_is_recorded_as_the_reason_the_body_is_incomplete() -> None:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})

        message = await receive()
        if message["type"] == "http.disconnect":
            return

        await send({"type": "http.response.body", "body": b"rest", "more_body": False})

    wrapped = ASGIMiddleware(app)
    server = _Server({"type": "http.disconnect"})

    with timeline() as tape:
        asyncio.run(wrapped(_scope(), server.receive, server.send))

    request = tape.all[0]
    assert request.data["disconnect"] is True
    assert request.data["incomplete"] is True
    assert request.result == "200 OK"
    assert request.items == 1


def test_a_pathsend_response_is_complete_but_uncounted() -> None:
    # The sendfile-style extension: forwarded untouched, settles
    # completeness, contributes nothing to items or bytes.

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.pathsend", "path": "/var/www/big.bin"})

    wrapped = ASGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        asyncio.run(wrapped(_scope(), server.receive, server.send))

    request = tape.all[0]
    assert "incomplete" not in request.data
    assert request.items == 0
    assert "bytes" not in request.data
    assert server.sent[-1]["type"] == "http.response.pathsend"


def test_an_unknown_status_falls_back_to_the_bare_number() -> None:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 599, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    wrapped = ASGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        asyncio.run(wrapped(_scope(), server.receive, server.send))

    assert tape.all[0].result == "599"


# ---------------------------------------------------------------------------
# redaction and capture
# ---------------------------------------------------------------------------


def test_sensitive_query_parameters_are_redacted_by_default() -> None:
    wrapped = ASGIMiddleware(application)
    server = _Server()

    with timeline() as tape:
        asyncio.run(
            wrapped(
                _scope(
                    query_string=(
                        b"limit=5&access_token=sekrit&ApiKey=k1"
                        b"&PHPSESSID=abc&session_id=xyz"
                    )
                ),
                server.receive,
                server.send,
            )
        )

    assert tape.all[0].data["query"] == (
        "limit=5&access_token=<redacted>&ApiKey=<redacted>"
        "&PHPSESSID=<redacted>&session_id=<redacted>"
    )


def test_redact_names_query_parameters() -> None:
    app = binding(__name__, "application", mode="asgi", capture=redact("signature"))

    with app, timeline() as tape:
        _serve(_scope(query_string=b"signature=abc123&limit=5&token=t"))

    assert tape.all[0].data["query"] == (
        "signature=<redacted>&limit=5&token=<redacted>"
    )


def test_redact_names_pertain_to_query_parameters_only() -> None:
    # A redacted name matches query string parameters, never the data
    # fields: redact("path") blanks a parameter named path while the
    # recorded URL path stays intact.

    app = binding(
        __name__, "application", mode="asgi", capture=redact("path", "method")
    )

    with app, timeline() as tape:
        _serve(_scope(query_string=b"path=secret&limit=5"))

    request = tape.all[0]
    assert request.data["path"] == "/orders/42"
    assert request.data["method"] == "GET"
    assert request.data["query"] == "path=<redacted>&limit=5"


def test_none_capture_omits_the_request_values() -> None:
    app = binding(__name__, "application", mode="asgi", capture="none")

    with app, timeline() as tape:
        _serve(_scope(query_string=b"access_token=x"))

    request = tape.all[0]
    assert request.data["interface"] == "asgi"
    assert "method" not in request.data
    assert "path" not in request.data
    assert "query" not in request.data
    assert request.result is wrapture.MISSING


# ---------------------------------------------------------------------------
# mode="asgi" bindings
# ---------------------------------------------------------------------------


def test_asgi_mode_installs_and_removes_the_middleware() -> None:
    module = sys.modules[__name__]
    original = module.application

    app = binding(__name__, "application", mode="asgi")

    with app:
        assert app.active
        assert isinstance(module.application, ASGIMiddleware)

    assert module.application is original


def test_asgi_mode_is_never_detected() -> None:
    app = binding(__name__, "application")

    assert app.mode == "callable"


def test_suspended_binding_passes_straight_through() -> None:
    app = binding(__name__, "application", mode="asgi")

    with app, timeline() as tape:
        app.suspend()
        server = _serve(_scope())

    assert server.body == b"hello world"
    assert "request" not in [event.kind for event in tape.all]
    assert app.suspended_calls == 1


def test_when_declines_recording_but_the_app_still_runs() -> None:
    app = binding(
        __name__,
        "application",
        mode="asgi",
        when=lambda _, args, kwargs: args[0]["path"].startswith("/api"),
    )

    with app, timeline() as tape:
        server = _serve(_scope())

    assert server.body == b"hello world"
    assert "request" not in [event.kind for event in tape.all]
    assert app.filtered_calls == 1


def test_when_false_makes_a_behaviour_only_asgi_binding() -> None:
    app = binding(__name__, "application", mode="asgi", when=False)
    app.on_request.transforms_response(lambda status, headers: (410, headers))

    with app, timeline() as tape:
        server = _serve(_scope())

    assert server.body == b"hello world"
    assert server.status == 410
    assert "request" not in [event.kind for event in tape.all]
    assert app.filtered_calls == 0


def test_namespace_gating_points_across_the_modes() -> None:
    app = binding(__name__, "application", mode="asgi")

    with pytest.raises(WrongModeError, match="use on_request"):
        _ = app.on_call

    call = binding(Gateway, "charge")

    with pytest.raises(WrongModeError, match="use on_call"):
        _ = call.on_request


# ---------------------------------------------------------------------------
# on_request behaviour
# ---------------------------------------------------------------------------


def test_transforms_scope_shapes_what_the_app_sees() -> None:
    app = binding(__name__, "application", mode="asgi")

    def force_flag(scope: dict[str, Any]) -> dict[str, Any]:
        scope["state_flags"] = "beta"
        return scope

    app.on_request.transforms_scope(force_flag)

    seen_scope.clear()
    with app:
        _serve(_scope())

    assert seen_scope[0]["state_flags"] == "beta"


def test_transforms_response_rewrites_status_and_headers_in_native_types() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.transforms_response(
        lambda status, headers: (410, [*headers, (b"x-traced", b"yes")])
    )

    with app:
        server = _serve(_scope())

    assert server.body == b"hello world"
    assert server.status == 410
    assert (b"x-traced", b"yes") in server.headers


def test_transforms_body_rewrites_each_chunk() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.transforms_body(lambda chunk: chunk.upper())

    with app, timeline() as tape:
        server = _serve(_scope())

    assert server.body == b"HELLO WORLD"
    assert tape.all[0].data["bytes"] == 11
    assert tape.all[0].items == 2


def test_returns_serves_a_canned_response_without_the_app() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.returns(
        503,
        [("Content-Type", "text/plain")],
        [b"mainten", b"ance"],
    )
    charge = binding(Gateway, "charge")

    with app, charge, timeline() as tape:
        server = _serve(_scope())

    assert server.body == b"maintenance"
    assert server.status == 503
    assert (b"content-type", b"text/plain") in [
        (name.lower(), value) for name, value in server.headers
    ]

    # The final body message and only it is flagged final.

    bodies = [m for m in server.sent if m["type"] == "http.response.body"]
    assert [m["more_body"] for m in bodies] == [True, False]

    # The app never ran: no observed call beneath the request, and the
    # canned outcome is marked injected.

    assert [event.kind for event in tape.all] == ["request"]
    assert tape.all[0].injected is True
    assert tape.all[0].result == "503 Service Unavailable"


def test_returns_wraps_a_bare_byte_string_as_one_chunk() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.returns(200, [], b"whole")

    with app, timeline() as tape:
        server = _serve(_scope())

    assert server.body == b"whole"
    assert tape.all[0].items == 1


def test_returns_accepts_a_status_line_string_for_convenience() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.returns("503 Service Unavailable", [], [b"down"])

    with app:
        server = _serve(_scope())

    assert server.status == 503


def test_returns_with_an_empty_body_still_completes_the_response() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.returns(204)

    with app, timeline() as tape:
        server = _serve(_scope())

    assert server.body == b""

    bodies = [m for m in server.sent if m["type"] == "http.response.body"]
    assert [m["more_body"] for m in bodies] == [False]
    assert "incomplete" not in tape.all[0].data


def test_raises_makes_the_server_see_the_app_fail() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.raises(ConnectionResetError("backend gone"))

    with app, timeline() as tape:
        with pytest.raises(ConnectionResetError, match="backend gone"):
            _serve(_scope())

    request = tape.all[0]
    assert request.injected is True
    assert isinstance(request.exception, ConnectionResetError)


def test_decorates_takes_custody_of_the_request() -> None:
    async def short_circuit(
        app: Any, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["path"] == "/health":
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        await app(scope, receive, send)

    app = binding(__name__, "application", mode="asgi")
    app.on_request.decorates(short_circuit)

    with app:
        server = _serve(_scope(path="/health"))
        assert server.body == b""
        assert server.status == 204

        server = _serve(_scope())
        assert server.body == b"hello world"
        assert server.status == 200


def test_behaviour_applies_when_nothing_is_recording() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.transforms_response(lambda status, headers: (410, headers))

    with app:
        server = _serve(_scope())

    assert server.body == b"hello world"
    assert server.status == 410


def test_passes_through_clears_request_behaviour() -> None:
    app = binding(__name__, "application", mode="asgi")
    app.on_request.returns(503, (), b"down")
    app.on_request.passes_through()

    with app:
        server = _serve(_scope())

    assert server.body == b"hello world"
    assert server.status == 200


# ---------------------------------------------------------------------------
# config reachability
# ---------------------------------------------------------------------------


def test_observe_entry_accepts_asgi_mode_with_name_only() -> None:
    entry = ObserveEntry(target=__name__, name="application", mode="asgi")
    config = Config(observe=[entry])

    applied = config.apply()
    try:
        assert isinstance(sys.modules[__name__].application, ASGIMiddleware)
    finally:
        applied.revert()


def test_observe_entry_rejects_asgi_mode_with_match() -> None:
    with pytest.raises(ConfigError, match="mode requires name"):
        ObserveEntry(target=__name__, match="app*", mode="asgi")


# ---------------------------------------------------------------------------
# the standalone recording options
# ---------------------------------------------------------------------------


def test_standalone_when_takes_a_request_filter() -> None:
    # A filter_requests() filter names requests not to record; the
    # application still runs and answers, matching when= everywhere:
    # the predicate decides recording only.

    wrapped = ASGIMiddleware(
        application, when=filter_requests(ignore={"path": ["/health", "/static/*"]})
    )

    with timeline() as tape:
        server = _Server()
        asyncio.run(wrapped(_scope(path="/health"), server.receive, server.send))
        assert server.body == b"hello world"

        server = _Server()
        asyncio.run(wrapped(_scope(path="/orders/1"), server.receive, server.send))

    (event,) = tape.all
    assert event.data["path"] == "/orders/1"


def test_standalone_when_callable_sees_the_scope() -> None:
    wrapped = ASGIMiddleware(
        application, when=lambda scope: scope["path"].startswith("/orders")
    )

    with timeline() as tape:
        server = _Server()
        asyncio.run(wrapped(_scope(path="/metrics"), server.receive, server.send))
        server = _Server()
        asyncio.run(wrapped(_scope(path="/orders/9"), server.receive, server.send))

    (event,) = tape.all
    assert event.data["path"] == "/orders/9"


def test_standalone_capture_args_redacts_query_parameters() -> None:
    wrapped = ASGIMiddleware(application, capture_args=redact("voucher"))

    with timeline() as tape:
        server = _Server()
        asyncio.run(
            wrapped(
                _scope(query_string=b"voucher=SECRET50&limit=5"),
                server.receive,
                server.send,
            )
        )

    assert tape.all[0].data["query"] == "voucher=<redacted>&limit=5"


def test_standalone_capture_result_none_omits_the_status_result() -> None:
    from wrapt import MISSING

    wrapped = ASGIMiddleware(application, capture_result="none")

    with timeline() as tape:
        server = _Server()
        asyncio.run(wrapped(_scope(), server.receive, server.send))

    event = tape.all[0]
    assert event.result is MISSING
    assert event.finished


# ---------------------------------------------------------------------------
# seed data
# ---------------------------------------------------------------------------


def test_seed_data_starts_the_request_event_and_never_its_own_fields() -> None:
    wrapped = ASGIMiddleware(application, data={"team": "shop", "method": "FAKE"})
    server = _Server()

    with timeline() as tape:
        asyncio.run(wrapped(_scope(), server.receive, server.send))

    request = tape.all[0]
    assert request.data["team"] == "shop"
    assert request.data["method"] == "GET"


# ---------------------------------------------------------------------------
# one boundary per request
# ---------------------------------------------------------------------------


def test_a_doubly_wrapped_application_records_one_request() -> None:
    # Interposition can wrap an app a framework's instrumentation
    # already wrapped; the same request must not record twice. The
    # outer middleware marks the scope and the inner passes through.

    wrapped = ASGIMiddleware(ASGIMiddleware(application))
    server = _Server()
    scope = _scope()

    with timeline() as tape:
        asyncio.run(wrapped(scope, server.receive, server.send))

    requests = [event for event in tape.all if event.kind == "request"]
    assert len(requests) == 1
    assert scope["wrapture.request"] is True
