"""Tests for the WSGI middleware, mode="wsgi" bindings and on_request.

The middleware is driven directly with hand-built environs and a
collecting start_response, playing the server's part, including its
obligation to call close() on the returned iterable. The protocol
obligations from PEP 3333 each get a test: start_response forwarding
with exc_info re-invocation, close() propagation on normal exhaustion,
failure and abandonment, and the untouched fast path when nothing is
recording.
"""

import sys
from typing import Any

import pytest

import wrapture
from wrapture import (
    Config,
    ConfigError,
    ObserveEntry,
    WrongModeError,
    WSGIMiddleware,
    binding,
    iterator,
    redact,
    timeline,
)


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


gateway = Gateway()

seen_environ: list[dict[str, Any]] = []


def application(environ: dict[str, Any], start_response: Any) -> Any:
    """A small streaming app: one observed call, then two body chunks."""

    seen_environ.append(environ)
    gateway.charge(42)

    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "10")])

    def body() -> Any:
        yield b"hello "
        yield b"world"

    return body()


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


class _Server:
    """The consuming half of the protocol: start_response plus close()."""

    def __init__(self) -> None:
        self.status: str | None = None
        self.headers: list[tuple[str, str]] | None = None
        self.exc_info: Any = None

    def start_response(
        self, status: str, headers: list[tuple[str, str]], exc_info: Any = None
    ) -> Any:
        self.status = status
        self.headers = headers
        self.exc_info = exc_info

    def consume(self, result: Any) -> bytes:
        try:
            return b"".join(result)
        finally:
            if hasattr(result, "close"):
                result.close()


def _serve(environ: dict[str, Any]) -> tuple[bytes, _Server]:
    # Call whatever currently sits at this module's application
    # attribute, middleware included, the way a server would.

    server = _Server()
    body = server.consume(
        sys.modules[__name__].application(environ, server.start_response)
    )
    return body, server


# ---------------------------------------------------------------------------
# the middleware standalone
# ---------------------------------------------------------------------------


def test_request_records_one_event_with_the_http_details() -> None:
    wrapped = WSGIMiddleware(application)
    server = _Server()

    with timeline() as tape:
        body = server.consume(
            wrapped(_environ(QUERY_STRING="expand=items"), server.start_response)
        )

    assert body == b"hello world"
    assert server.status == "200 OK"

    request = tape.all[0]
    assert request.kind == "request"
    assert request.result == "200 OK"
    assert request.items == 2
    assert request.data["interface"] == "wsgi"
    assert request.data["method"] == "GET"
    assert request.data["path"] == "/orders/42"
    assert request.data["query"] == "expand=items"
    assert request.data["scheme"] == "http"
    assert request.data["protocol"] == "HTTP/1.1"
    assert request.data["remote"] == "127.0.0.1"
    assert request.data["content_type"] == "text/plain"
    assert request.data["content_length"] == 10
    assert request.data["bytes"] == 11
    assert request.data["app_duration"] is not None
    assert request.duration is not None
    assert request.body_duration is not None


def test_request_line_display() -> None:
    wrapped = WSGIMiddleware(application)
    server = _Server()

    with timeline() as tape:
        server.consume(
            wrapped(_environ(QUERY_STRING="expand=items"), server.start_response)
        )

    assert str(tape.all[0]) == (f"GET /orders/42?expand=items ({__name__}:application)")


def test_calls_nest_under_the_request() -> None:
    wrapped = WSGIMiddleware(application)
    server = _Server()
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        server.consume(wrapped(_environ(), server.start_response))

    request, call = tape.all
    assert request.kind == "request"
    assert call.kind == "call"
    assert call.parent_id == request.seq


def test_work_done_while_streaming_nests_under_the_request() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [])

        def body() -> Any:
            yield b"first"
            gateway.charge(7)
            yield b"second"

        return body()

    wrapped = WSGIMiddleware(app)
    server = _Server()
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        server.consume(wrapped(_environ(), server.start_response))

    request, call = tape.all
    assert call.parent_id == request.seq


def test_not_recording_returns_the_iterable_untouched() -> None:
    # The fast path hands back exactly what the application returned,
    # which is what keeps wsgi.file_wrapper meaningful.

    marker = [b"untouched"]

    def app(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [])
        return marker

    wrapped = WSGIMiddleware(app)
    server = _Server()

    assert wrapped(_environ(), server.start_response) is marker


def test_app_error_in_the_synchronous_phase_is_recorded_and_raised() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> Any:
        raise RuntimeError("app exploded")

    wrapped = WSGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        with pytest.raises(RuntimeError, match="app exploded"):
            wrapped(_environ(), server.start_response)

    request = tape.all[0]
    assert isinstance(request.exception, RuntimeError)
    assert request.duration is not None


def test_error_while_streaming_is_recorded_and_raised() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [])

        def body() -> Any:
            yield b"partial"
            raise OSError("pipe burst")

        return body()

    wrapped = WSGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        result = wrapped(_environ(), server.start_response)
        with pytest.raises(OSError, match="pipe burst"):
            server.consume(result)

    request = tape.all[0]
    assert isinstance(request.exception, OSError)
    assert request.items == 1


def test_close_after_partial_iteration_marks_the_body_incomplete() -> None:
    # A client disconnect: the server closes the iterable mid-stream.

    wrapped = WSGIMiddleware(application)
    server = _Server()

    with timeline() as tape:
        result = wrapped(_environ(), server.start_response)
        next(iter(result))
        result.close()

    request = tape.all[0]
    assert request.items == 1
    assert request.data["incomplete"] is True
    assert request.result == "200 OK"
    assert request.duration is not None


def test_close_propagates_to_the_wrapped_iterable_exactly_once() -> None:
    closes: list[str] = []

    class Body:
        def __init__(self) -> None:
            self._chunks = iter([b"one", b"two"])

        def __iter__(self) -> Any:
            return self

        def __next__(self) -> bytes:
            return next(self._chunks)

        def close(self) -> None:
            closes.append("closed")

    def app(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [])
        return Body()

    wrapped = WSGIMiddleware(app)
    server = _Server()

    with timeline():
        result = wrapped(_environ(), server.start_response)
        server.consume(result)
        result.close()

    assert closes == ["closed"]


def test_an_exception_from_close_is_recorded_and_raised() -> None:
    class Body:
        def __iter__(self) -> Any:
            return iter([b"chunk"])

        def close(self) -> None:
            raise ValueError("close failed")

    def app(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [])
        return Body()

    wrapped = WSGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        result = wrapped(_environ(), server.start_response)
        next(iter(result))
        with pytest.raises(ValueError, match="close failed"):
            result.close()

    request = tape.all[0]
    assert isinstance(request.exception, ValueError)


def test_a_never_closed_response_leaves_the_event_visibly_open() -> None:
    wrapped = WSGIMiddleware(application)
    server = _Server()

    with timeline() as tape:
        result = wrapped(_environ(), server.start_response)
        next(iter(result))

    request = tape.all[0]
    assert request.duration is None
    assert request.result is wrapture.MISSING


def test_exc_info_reinvocation_replaces_the_recorded_status() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [("Content-Type", "text/plain")])
        try:
            raise RuntimeError("late failure")
        except RuntimeError:
            start_response(
                "500 Internal Server Error",
                [("Content-Type", "text/plain")],
                sys.exc_info(),
            )
        return [b"error body"]

    wrapped = WSGIMiddleware(app)
    server = _Server()

    with timeline() as tape:
        server.consume(wrapped(_environ(), server.start_response))

    assert server.status == "500 Internal Server Error"
    assert server.exc_info is not None
    assert tape.all[0].result == "500 Internal Server Error"


# ---------------------------------------------------------------------------
# redaction and capture
# ---------------------------------------------------------------------------


def test_sensitive_query_parameters_are_redacted_by_default() -> None:
    wrapped = WSGIMiddleware(application)
    server = _Server()

    with timeline() as tape:
        server.consume(
            wrapped(
                _environ(
                    QUERY_STRING=(
                        "limit=5&access_token=sekrit&ApiKey=k1"
                        "&PHPSESSID=abc&session_id=xyz"
                        "&X-Amz-Signature=s1&sig=s2"
                    )
                ),
                server.start_response,
            )
        )

    assert tape.all[0].data["query"] == (
        "limit=5&access_token=<redacted>&ApiKey=<redacted>"
        "&PHPSESSID=<redacted>&session_id=<redacted>"
        "&X-Amz-Signature=<redacted>&sig=<redacted>"
    )


def test_redact_names_query_parameters() -> None:
    app = binding(__name__, "application", mode="wsgi", capture=redact("signature"))

    with app, timeline() as tape:
        _serve(_environ(QUERY_STRING="signature=abc123&limit=5&token=t"))

    assert tape.all[0].data["query"] == (
        "signature=<redacted>&limit=5&token=<redacted>"
    )


def test_redact_names_pertain_to_query_parameters_only() -> None:
    # A redacted name matches query string parameters, never the data
    # fields: redact("path") blanks a parameter named path while the
    # recorded URL path stays intact.

    app = binding(
        __name__, "application", mode="wsgi", capture=redact("path", "method")
    )

    with app, timeline() as tape:
        _serve(_environ(QUERY_STRING="path=secret&limit=5"))

    request = tape.all[0]
    assert request.data["path"] == "/orders/42"
    assert request.data["method"] == "GET"
    assert request.data["query"] == "path=<redacted>&limit=5"


def test_none_capture_omits_the_request_values() -> None:
    app = binding(__name__, "application", mode="wsgi", capture="none")

    with app, timeline() as tape:
        _serve(_environ(QUERY_STRING="access_token=x"))

    request = tape.all[0]
    assert request.data["interface"] == "wsgi"
    assert "method" not in request.data
    assert "path" not in request.data
    assert "query" not in request.data
    assert request.result is wrapture.MISSING


# ---------------------------------------------------------------------------
# mode="wsgi" bindings
# ---------------------------------------------------------------------------


def test_wsgi_mode_installs_and_removes_the_middleware() -> None:
    module = sys.modules[__name__]
    original = module.application

    app = binding(__name__, "application", mode="wsgi")

    with app:
        assert app.active
        assert isinstance(module.application, WSGIMiddleware)

    assert module.application is original


def test_wsgi_mode_is_never_detected() -> None:
    app = binding(__name__, "application")

    assert app.mode == "callable"


def test_suspended_binding_passes_straight_through() -> None:
    app = binding(__name__, "application", mode="wsgi")

    with app, timeline() as tape:
        app.suspend()
        body, _ = _serve(_environ())

    assert body == b"hello world"
    assert "request" not in [event.kind for event in tape.all]
    assert app.suspended_calls == 1


def test_when_declines_recording_but_the_app_still_runs() -> None:
    app = binding(
        __name__,
        "application",
        mode="wsgi",
        when=lambda _, args, kwargs: args[0]["PATH_INFO"].startswith("/api"),
    )

    with app, timeline() as tape:
        body, _ = _serve(_environ())

    assert body == b"hello world"
    assert "request" not in [event.kind for event in tape.all]
    assert app.filtered_calls == 1


def test_when_false_makes_a_behaviour_only_wsgi_binding() -> None:
    app = binding(__name__, "application", mode="wsgi", when=False)
    app.on_request.transforms_response(lambda status, headers: ("410 Gone", headers))

    with app, timeline() as tape:
        body, server = _serve(_environ())

    assert body == b"hello world"
    assert server.status == "410 Gone"
    assert "request" not in [event.kind for event in tape.all]
    assert app.filtered_calls == 0


def test_namespace_gating_points_across_the_modes() -> None:
    app = binding(__name__, "application", mode="wsgi")

    with pytest.raises(WrongModeError, match="use on_request"):
        _ = app.on_call

    call = binding(Gateway, "charge")

    with pytest.raises(WrongModeError, match="use on_call"):
        _ = call.on_request


# ---------------------------------------------------------------------------
# on_request behaviour
# ---------------------------------------------------------------------------


def test_transforms_environ_shapes_what_the_app_sees() -> None:
    app = binding(__name__, "application", mode="wsgi")

    def force_flag(environ: dict[str, Any]) -> dict[str, Any]:
        environ["HTTP_X_FLAGS"] = "beta"
        return environ

    app.on_request.transforms_environ(force_flag)

    seen_environ.clear()
    with app:
        _serve(_environ())

    assert seen_environ[0]["HTTP_X_FLAGS"] == "beta"


def test_transforms_response_rewrites_status_and_headers() -> None:
    app = binding(__name__, "application", mode="wsgi")
    app.on_request.transforms_response(
        lambda status, headers: ("410 Gone", [*headers, ("X-Traced", "yes")])
    )

    with app:
        body, server = _serve(_environ())

    assert body == b"hello world"
    assert server.status == "410 Gone"
    assert server.headers is not None
    assert ("X-Traced", "yes") in server.headers


def test_transforms_body_composes_with_iterator() -> None:
    app = binding(__name__, "application", mode="wsgi")
    app.on_request.transforms_body(
        iterator().on_item.transforms_item(lambda chunk: chunk.upper())
    )

    with app, timeline() as tape:
        body, _ = _serve(_environ())

    assert body == b"HELLO WORLD"
    assert tape.all[0].data["bytes"] == 11


def test_returns_serves_a_canned_response_without_the_app() -> None:
    app = binding(__name__, "application", mode="wsgi")
    app.on_request.returns(
        "503 Service Unavailable",
        [("Content-Type", "text/plain")],
        [b"mainten", b"ance"],
    )
    charge = binding(Gateway, "charge")

    with app, charge, timeline() as tape:
        body, server = _serve(_environ())

    assert body == b"maintenance"
    assert server.status == "503 Service Unavailable"

    # The app never ran: no observed call beneath the request, and the
    # canned outcome is marked injected.

    assert [event.kind for event in tape.all] == ["request"]
    assert tape.all[0].injected is True


def test_returns_wraps_a_bare_byte_string_as_one_chunk() -> None:
    # The convenience form: bare bytes are served as a single chunk,
    # never iterated element by element.

    app = binding(__name__, "application", mode="wsgi")
    app.on_request.returns("200 OK", [], b"whole")

    with app, timeline() as tape:
        body, _ = _serve(_environ())

    assert body == b"whole"
    assert tape.all[0].items == 1


def test_raises_makes_the_server_see_the_app_fail() -> None:
    app = binding(__name__, "application", mode="wsgi")
    app.on_request.raises(ConnectionResetError("backend gone"))

    server = _Server()

    with app, timeline() as tape:
        with pytest.raises(ConnectionResetError, match="backend gone"):
            sys.modules[__name__].application(_environ(), server.start_response)

    request = tape.all[0]
    assert request.injected is True
    assert isinstance(request.exception, ConnectionResetError)


def test_decorates_takes_custody_of_the_request() -> None:
    def short_circuit(app: Any, environ: dict[str, Any], start_response: Any) -> Any:
        if environ["PATH_INFO"] == "/health":
            start_response("204 No Content", [])
            return []
        return app(environ, start_response)

    app = binding(__name__, "application", mode="wsgi")
    app.on_request.decorates(short_circuit)

    with app:
        body, server = _serve(_environ(PATH_INFO="/health"))
        assert body == b""
        assert server.status == "204 No Content"

        body, server = _serve(_environ())
        assert body == b"hello world"
        assert server.status == "200 OK"


def test_behaviour_applies_when_nothing_is_recording() -> None:
    app = binding(__name__, "application", mode="wsgi")
    app.on_request.transforms_response(lambda status, headers: ("410 Gone", headers))

    with app:
        body, server = _serve(_environ())

    assert body == b"hello world"
    assert server.status == "410 Gone"


def test_passes_through_clears_request_behaviour() -> None:
    app = binding(__name__, "application", mode="wsgi")
    app.on_request.returns("503 Service Unavailable", (), b"down")
    app.on_request.passes_through()

    with app:
        body, server = _serve(_environ())

    assert body == b"hello world"
    assert server.status == "200 OK"


# ---------------------------------------------------------------------------
# config reachability
# ---------------------------------------------------------------------------


def test_observe_entry_accepts_wsgi_mode_with_name_only() -> None:
    entry = ObserveEntry(target=__name__, name="application", mode="wsgi")
    config = Config(observe=[entry])

    applied = config.apply()
    try:
        assert isinstance(sys.modules[__name__].application, WSGIMiddleware)
    finally:
        applied.revert()


def test_observe_entry_rejects_wsgi_mode_with_match() -> None:
    with pytest.raises(ConfigError, match="mode requires name"):
        ObserveEntry(target=__name__, match="app*", mode="wsgi")


def test_observe_entry_rejects_unknown_modes() -> None:
    with pytest.raises(ConfigError, match="mode must be omitted, 'wsgi' or 'asgi'"):
        ObserveEntry(target=__name__, name="application", mode="grpc")


# ---------------------------------------------------------------------------
# the standalone recording options
# ---------------------------------------------------------------------------


def test_standalone_when_globs_skip_matching_paths() -> None:
    # A glob list names paths not to record; the application still
    # runs and answers, matching when= everywhere: the predicate
    # decides recording only.

    wrapped = WSGIMiddleware(application, when=["/health", "/static/*"])

    with timeline() as tape:
        for path in ("/health", "/static/logo.png"):
            server = _Server()
            body = server.consume(
                wrapped(_environ(PATH_INFO=path), server.start_response)
            )
            assert body == b"hello world"
            assert server.status == "200 OK"

        server = _Server()
        server.consume(wrapped(_environ(PATH_INFO="/orders/1"), server.start_response))

    (event,) = tape.all
    assert event.data["path"] == "/orders/1"


def test_standalone_when_takes_a_single_glob_string() -> None:
    wrapped = WSGIMiddleware(application, when="/health")

    with timeline() as tape:
        server = _Server()
        server.consume(wrapped(_environ(PATH_INFO="/health"), server.start_response))

    assert tape.all == []


def test_standalone_when_callable_sees_the_environ() -> None:
    # The standalone callable takes the environ directly, unlike the
    # binding-internal convention.

    wrapped = WSGIMiddleware(
        application, when=lambda environ: environ["PATH_INFO"].startswith("/orders")
    )

    with timeline() as tape:
        server = _Server()
        server.consume(wrapped(_environ(PATH_INFO="/metrics"), server.start_response))
        server = _Server()
        server.consume(wrapped(_environ(PATH_INFO="/orders/9"), server.start_response))

    (event,) = tape.all
    assert event.data["path"] == "/orders/9"


def test_standalone_when_rejects_a_bad_value() -> None:
    with pytest.raises(ValueError, match="glob string"):
        WSGIMiddleware(application, when=42)


def test_standalone_options_win_over_the_binding() -> None:
    # An explicit option beats the bound binding's value: the binding
    # says never record, the constructor's predicate says record.

    silent = binding(__name__, "application", mode="wsgi", when=False)
    wrapped = WSGIMiddleware(application, binding=silent, when=lambda environ: True)

    with timeline() as tape:
        server = _Server()
        server.consume(wrapped(_environ(), server.start_response))

    assert len(tape.all) == 1


def test_standalone_capture_args_redacts_query_parameters() -> None:
    wrapped = WSGIMiddleware(application, capture_args=redact("voucher"))

    with timeline() as tape:
        server = _Server()
        server.consume(
            wrapped(
                _environ(QUERY_STRING="voucher=SECRET50&limit=5"),
                server.start_response,
            )
        )

    assert tape.all[0].data["query"] == "voucher=<redacted>&limit=5"


def test_standalone_capture_result_none_omits_the_status_result() -> None:
    from wrapt import MISSING

    wrapped = WSGIMiddleware(application, capture_result="none")

    with timeline() as tape:
        server = _Server()
        server.consume(wrapped(_environ(), server.start_response))

    event = tape.all[0]
    assert event.result is MISSING
    assert event.finished


# ---------------------------------------------------------------------------
# seed data
# ---------------------------------------------------------------------------


def test_seed_data_starts_the_request_event() -> None:
    app = binding(__name__, "application", mode="wsgi", data={"team": "shop"})

    with app, timeline() as tape:
        _serve(_environ())

    assert tape.all[0].data["team"] == "shop"


def test_the_requests_own_fields_win_over_a_seeded_reserved_key() -> None:
    # A declaration cannot override what the middleware knows about
    # the request: the seed goes in first and the request's fields
    # are written over it.

    app = binding(__name__, "application", mode="wsgi", data={"method": "FAKE"})

    with app, timeline() as tape:
        _serve(_environ())

    assert tape.all[0].data["method"] == "GET"


def test_the_standalone_middleware_takes_seed_data() -> None:
    wrapped = wrapture.WSGIMiddleware(application, data={"team": "shop"})

    with timeline() as tape:
        body = wrapped(_environ(), lambda status, headers: None)
        list(body)
        body.close()

    assert tape.all[0].data["team"] == "shop"
