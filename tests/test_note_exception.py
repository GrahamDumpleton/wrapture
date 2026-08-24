"""Tests for note_exception() and the caught-exception model.

A framework that catches an exception and hands it to a handler leaves
the scope that failed completing with a result and no exception; the
handler is the only code that can be bound, and note_exception() is
how it reports the failure against the event that actually failed.
These tests cover the call, the current_event() filters it aims with,
the identity rules, and what the tape, the filters and the renderers
make of a noted exception.
"""

import io
import threading
import warnings
from typing import Any

import pytest

import wrapture
from wrapture import (
    CaughtException,
    ConfigWarning,
    Printer,
    WSGIMiddleware,
    binding,
    current_event,
    note_exception,
    propagate,
    timeline,
)
from wrapture.sinks import _scoped_sinks


class Pricing:
    def quote(self, sku: str) -> int:
        if sku == "missing":
            raise KeyError(sku)
        return 100


class Shop:
    """The framework shape: dispatch() catches whatever the view
    raises and hands it to handle_error(), which returns normally."""

    def __init__(self) -> None:
        self.pricing = Pricing()

    def dispatch(self, sku: str) -> str:
        try:
            return f"200 {self.pricing.quote(sku)}"
        except Exception as exc:
            return self.handle_error(exc)

    def handle_error(self, exc: BaseException) -> str:
        return "500"

    def retry(self, sku: str) -> str:
        # Two attempts, each failure handled, both noted against the
        # same unit of work.

        for _ in range(2):
            try:
                return f"200 {self.pricing.quote(sku)}"
            except Exception as exc:
                self.handle_error(exc)

        return "503"


def _replacing(fn: Any) -> Any:
    # A decorates() terminal that stands in for the real method:
    # fn(instance, *args, **kwargs), the real callable never invoked.

    def terminal(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        return fn(instance, *args, **kwargs)

    return terminal


# ---------------------------------------------------------------------------
# the call
# ---------------------------------------------------------------------------


def test_note_exception_is_a_no_op_when_nothing_is_recording() -> None:
    note_exception(KeyError("nothing listening"))

    assert not current_event()


def test_note_exception_attaches_to_the_in_flight_event() -> None:
    dispatch = binding(Shop, "dispatch")
    error = KeyError("missing")

    def noting(exc: BaseException) -> str:
        note_exception(exc)
        return "500"

    handle = binding(Shop, "handle_error").on_call.decorates(
        _replacing(lambda self, exc: noting(exc))
    )

    with timeline(dispatch, handle) as tape:
        Shop().handle_error(error)

    event = tape.for_binding(handle).first
    assert event.failed
    assert len(event.caught) == 1
    assert isinstance(event.caught[0], CaughtException)
    assert event.caught[0].exception is error
    assert event.exception is None
    assert event.result == "500"


def test_note_exception_records_the_moment_of_the_note() -> None:
    dispatch = binding(Shop, "dispatch")

    def noting(exc: BaseException) -> str:
        note_exception(exc)
        return "500"

    handle = binding(Shop, "handle_error").on_call.decorates(
        _replacing(lambda self, exc: noting(exc))
    )

    with timeline(dispatch, handle) as tape:
        Shop().dispatch("missing")

    handler = tape.for_binding(handle).first
    assert handler.started is not None
    assert handler.caught[0].at >= handler.started


def test_note_exception_aims_through_a_current_event_handle() -> None:
    dispatch = binding(Shop, "dispatch")
    quote = binding(Pricing, "quote")

    def noting(exc: BaseException) -> str:
        current_event(binding=dispatch).note_exception(exc)
        return "500"

    handle = binding(Shop, "handle_error").on_call.decorates(
        _replacing(lambda self, exc: noting(exc))
    )

    with timeline(dispatch, quote, handle) as tape:
        assert Shop().dispatch("missing") == "500"

    request = tape.for_binding(dispatch).first
    assert request.result == "500"
    assert request.exception is None
    assert [type(c.exception) for c in request.caught] == [KeyError]

    # The handler's own event, the in-flight one, carries nothing:
    # the note was aimed past it.

    assert tape.for_binding(handle).first.caught == ()

    # The view that raised saw the exception escape; the unit of work
    # that caught it saw the note. Two scopes, both failed, for the
    # same reason.

    view = tape.for_binding(quote).first
    assert view.exception is request.caught[0].exception
    assert tape.for_binding(quote).raising(KeyError).count == 1
    assert tape.for_binding(dispatch).raising(KeyError).count == 1


# ---------------------------------------------------------------------------
# current_event() filters
# ---------------------------------------------------------------------------


def test_current_event_by_binding_returns_the_nearest_enclosing_match() -> None:
    dispatch = binding(Shop, "dispatch")
    quote = binding(Pricing, "quote")
    handle = binding(Shop, "handle_error")

    seen: dict[str, Any] = {}

    def look(self: Any, exc: BaseException) -> str:
        seen["innermost"] = current_event()
        seen["dispatch"] = current_event(binding=dispatch)
        seen["namespace"] = current_event(binding=dispatch.on_call)
        seen["quote"] = current_event(binding=quote)
        seen["handle"] = current_event(binding=handle)
        return "500"

    handle.on_call.decorates(_replacing(look))

    with timeline(dispatch, quote, handle) as tape:
        Shop().dispatch("missing")

    assert seen["innermost"] == tape.for_binding(handle).first
    assert seen["handle"] == seen["innermost"]
    assert seen["dispatch"] == tape.for_binding(dispatch).first

    # A behaviour namespace stands in for its binding, as elsewhere.

    assert seen["namespace"] == seen["dispatch"]

    # The view's event had already closed by the time the handler ran,
    # so it is not enclosing and does not match: the handle is empty.

    assert not seen["quote"]


def test_current_event_by_kind_returns_the_nearest_enclosing_match() -> None:
    seen: dict[str, Any] = {}

    def application(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        with wrapture.block("dispatch"):
            seen["request"] = current_event(kind="request")
            seen["block"] = current_event(kind="block")
            seen["call"] = current_event(kind="call")
            seen["innermost"] = current_event()
        start_response("200 OK", [])
        return [b""]

    wrapped = WSGIMiddleware(application)

    with timeline() as tape:
        body = wrapped(_environ(), lambda *a: None)
        body.close()

    request = tape.all[0]
    assert request.kind == "request"
    assert seen["request"] == request
    assert seen["block"] == seen["innermost"]
    assert seen["block"].kind == "block"
    assert not seen["call"]


def test_current_event_with_both_filters_needs_both_to_match() -> None:
    dispatch = binding(Shop, "dispatch")
    handle = binding(Shop, "handle_error")

    seen: dict[str, Any] = {}

    def look(self: Any, exc: BaseException) -> str:
        seen["both"] = current_event(kind="call", binding=dispatch)
        seen["wrong_kind"] = current_event(kind="request", binding=dispatch)
        return "500"

    handle.on_call.decorates(_replacing(look))

    with timeline(dispatch, handle) as tape:
        Shop().dispatch("missing")

    assert seen["both"] == tape.for_binding(dispatch).first
    assert not seen["wrong_kind"]


def test_current_event_is_an_empty_handle_outside_recording() -> None:
    # Empty handles are falsy, their verbs do nothing, and reading a
    # field names the filters that failed to match.

    assert not current_event(kind="request")
    assert not current_event(binding=object())

    current_event(kind="request").annotate(route="/ignored")

    with pytest.raises(AttributeError, match="kind='request'"):
        _ = current_event(kind="request").kind


def test_the_handle_is_read_only_outside_its_verbs() -> None:
    dispatch = binding(Shop, "dispatch")

    def look(self: Any, sku: str) -> str:
        handle = current_event()
        with pytest.raises(AttributeError, match="read-only"):
            handle.result = "tampered"
        return "200"

    dispatch.on_call.decorates(_replacing(look))

    with timeline(dispatch) as tape:
        Shop().dispatch("sku")

    assert tape.for_binding(dispatch).first.result == "200"


def test_a_handle_compares_and_hashes_as_its_event() -> None:
    dispatch = binding(Shop, "dispatch")
    stashed: list[Any] = []

    def look(self: Any, sku: str) -> str:
        stashed.append(current_event())
        stashed.append(current_event(kind="call"))
        return "200"

    dispatch.on_call.decorates(_replacing(look))

    with timeline(dispatch) as tape:
        Shop().dispatch("sku")

    event = tape.for_binding(dispatch).first
    first, second = stashed

    assert first == second
    assert first == event
    assert event == first
    assert hash(first) == hash(event)


def test_an_aimed_annotate_reaches_the_enclosing_event() -> None:
    dispatch = binding(Shop, "dispatch")
    quote = binding(Pricing, "quote")

    def noting(self: Any, sku: str) -> int:
        current_event(binding=dispatch).annotate(sku=sku, tier="gold")
        return 100

    quote.on_call.decorates(_replacing(noting))

    with timeline(dispatch, quote) as tape:
        Shop().dispatch("sku")

    outer = tape.for_binding(dispatch).first
    assert outer.data == {"sku": "sku", "tier": "gold"}
    assert tape.for_binding(quote).first.data == {}


# ---------------------------------------------------------------------------
# identity rules
# ---------------------------------------------------------------------------


def test_noting_the_same_exception_twice_records_it_once() -> None:
    dispatch = binding(Shop, "dispatch")
    error = KeyError("missing")

    def twice(self: Any, sku: str) -> str:
        note_exception(error)
        note_exception(error)
        return "500"

    dispatch.on_call.decorates(_replacing(twice))

    with timeline(dispatch) as tape:
        Shop().dispatch("missing")

    assert len(tape.for_binding(dispatch).first.caught) == 1


def test_distinct_exceptions_are_each_noted_in_order() -> None:
    dispatch = binding(Shop, "dispatch")
    first, second = KeyError("one"), ValueError("two")

    def both(self: Any, sku: str) -> str:
        note_exception(first)
        note_exception(second)
        return "500"

    dispatch.on_call.decorates(_replacing(both))

    with timeline(dispatch) as tape:
        Shop().dispatch("missing")

    caught = tape.for_binding(dispatch).first.caught
    assert [c.exception for c in caught] == [first, second]


def test_a_noted_exception_that_then_escapes_shows_once_as_the_escape() -> None:
    dispatch = binding(Shop, "dispatch")
    error = KeyError("missing")

    def note_then_raise(self: Any, sku: str) -> str:
        note_exception(error)
        raise error

    dispatch.on_call.decorates(_replacing(note_then_raise))

    with timeline(dispatch) as tape:
        with pytest.raises(KeyError):
            Shop().dispatch("missing")

    event = tape.for_binding(dispatch).first
    assert event.exception is error
    assert event.caught == ()
    assert event.failed
    assert tape.for_binding(dispatch).raising(KeyError).count == 1


def test_a_different_exception_escaping_keeps_the_note() -> None:
    dispatch = binding(Shop, "dispatch")
    noted = KeyError("missing")

    def note_then_raise_other(self: Any, sku: str) -> str:
        note_exception(noted)
        raise RuntimeError("gave up")

    dispatch.on_call.decorates(_replacing(note_then_raise_other))

    with timeline(dispatch) as tape:
        with pytest.raises(RuntimeError):
            Shop().dispatch("missing")

    event = tape.for_binding(dispatch).first
    assert isinstance(event.exception, RuntimeError)
    assert [c.exception for c in event.caught] == [noted]


def test_a_note_against_a_finished_event_is_refused_with_a_warning() -> None:
    # A handle can only be taken while the event is in flight, so the
    # finished case needs one stashed past its event's close.

    dispatch = binding(Shop, "dispatch")
    stashed: list[Any] = []

    def stash(self: Any, sku: str) -> str:
        stashed.append(current_event(binding=dispatch))
        return "200"

    dispatch.on_call.decorates(_replacing(stash))

    with timeline(dispatch) as tape:
        Shop().dispatch("sku")

    event = tape.for_binding(dispatch).first
    assert event.finished
    (handle,) = stashed
    assert handle == event

    with pytest.warns(ConfigWarning, match="already finished") as record:
        handle.note_exception(KeyError("late"))

    assert "Shop.dispatch" in str(record[0].message)
    assert "KeyError" in str(record[0].message)
    assert event.caught == ()
    assert not event.failed


def test_annotate_on_a_finished_event_is_refused_with_a_warning() -> None:
    dispatch = binding(Shop, "dispatch")
    stashed: list[Any] = []

    def stash(self: Any, sku: str) -> str:
        stashed.append(current_event(binding=dispatch))
        return "200"

    dispatch.on_call.decorates(_replacing(stash))

    with timeline(dispatch) as tape:
        Shop().dispatch("sku")

    event = tape.for_binding(dispatch).first
    (handle,) = stashed

    with pytest.warns(ConfigWarning, match="already finished"):
        handle.annotate(late=True)

    assert "late" not in event.data


def test_a_note_against_an_open_event_on_another_thread_attaches() -> None:
    dispatch = binding(Shop, "dispatch")
    error = TimeoutError("worker")
    opened = threading.Event()
    noted = threading.Event()
    stashed: list[Any] = []

    def slow(self: Any, sku: str) -> str:
        stashed.append(current_event(binding=dispatch))
        opened.set()
        noted.wait(timeout=5)
        return "200"

    dispatch.on_call.decorates(_replacing(slow))

    with timeline(dispatch) as tape:
        worker = threading.Thread(target=propagate(lambda: Shop().dispatch("sku")))
        worker.start()
        assert opened.wait(timeout=5)

        assert not tape.for_binding(dispatch).first.finished

        # The note comes from the main thread, through the handle the
        # worker took while its event was in flight; on_exit, when it
        # comes, carries it.

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            stashed[0].note_exception(error)

        noted.set()
        worker.join(timeout=5)

    event = tape.for_binding(dispatch).first
    assert event.finished
    assert event.result == "200"
    assert [c.exception for c in event.caught] == [error]


# ---------------------------------------------------------------------------
# the event, the filters and the renderers
# ---------------------------------------------------------------------------


def test_failed_distinguishes_nothing_from_escaped_from_noted() -> None:
    event = wrapture.Event("call", "tests:thing")
    assert not event.failed

    event.exception = KeyError("escaped")
    assert event.failed

    noted = wrapture.Event("call", "tests:thing")
    noted.caught = (CaughtException(KeyError("noted"), 0.0),)
    assert noted.failed
    assert noted.exception is None


def _noting_shop(*, retry_capture: str = "reference") -> tuple[Any, Any, Any]:
    # dispatch catches, handle_error notes against dispatch's event
    # (or retry's, whichever is in flight).

    dispatch = binding(Shop, "dispatch")
    retry = binding(Shop, "retry", capture=retry_capture)

    def noting(self: Any, exc: BaseException) -> str:
        # Empty handles are falsy, so `or` walks the candidates, and
        # the verbs no-op on an empty handle if neither matched.

        target = current_event(binding=dispatch) or current_event(binding=retry)
        target.note_exception(exc)
        return "500"

    handle = binding(Shop, "handle_error").on_call.decorates(_replacing(noting))

    return dispatch, retry, handle


def test_raising_matches_noted_exceptions_with_and_without_a_type() -> None:
    dispatch, retry, handle = _noting_shop()

    with timeline(dispatch, retry, handle) as tape:
        Shop().dispatch("missing")
        Shop().dispatch("sku")

    log = tape.for_binding(dispatch)
    assert log.count == 2
    assert log.raising().count == 1
    assert log.raising(KeyError).count == 1
    assert log.raising(ValueError).count == 0
    assert log.raising(KeyError).first.result == "500"

    # The filtered log's label still says raising: the exception was
    # raised inside the scope, whether or not it escaped.

    assert log.raising(KeyError).label.endswith("[raising=KeyError]")


def test_tree_marks_noted_exceptions_after_the_result() -> None:
    dispatch, retry, handle = _noting_shop()
    quote = binding(Pricing, "quote")

    with timeline(dispatch, retry, quote, handle) as tape:
        Shop().dispatch("missing")

    assert tape.tree() == (
        "test_note_exception:Shop.dispatch(sku='missing')  -> '500'  !! KeyError\n"
        "  test_note_exception:Pricing.quote(sku='missing')  !! KeyError\n"
        "  test_note_exception:Shop.handle_error(exc=KeyError('missing'))"
        "  -> '500'"
    )


def test_tree_marks_noted_exceptions_without_a_result_and_in_order() -> None:
    dispatch, retry, handle = _noting_shop(retry_capture="none")

    with timeline(dispatch, retry, handle) as tape:
        Shop().retry("missing")

    lines = tape.tree().splitlines()
    assert lines[0] == "test_note_exception:Shop.retry()  !! KeyError  !! KeyError"


def test_printer_marks_noted_exceptions_on_the_closing_line() -> None:
    dispatch, retry, handle = _noting_shop()
    output = io.StringIO()

    token = _scoped_sinks.set(_scoped_sinks.get() + (Printer(output, timing=False),))
    try:
        with dispatch, handle:
            Shop().dispatch("missing")
    finally:
        _scoped_sinks.reset(token)

    assert output.getvalue() == (
        "test_note_exception:Shop.dispatch(sku='missing')\n"
        "  test_note_exception:Shop.handle_error(exc=KeyError('missing'))\n"
        "  test_note_exception:Shop.handle_error -> '500'\n"
        "test_note_exception:Shop.dispatch -> '500' !! KeyError\n"
    )


def test_printer_writes_a_closing_line_for_a_note_with_no_result() -> None:
    dispatch, retry, handle = _noting_shop(retry_capture="none")
    output = io.StringIO()

    token = _scoped_sinks.set(_scoped_sinks.get() + (Printer(output, timing=False),))
    try:
        with retry, handle:
            Shop().retry("missing")
    finally:
        _scoped_sinks.reset(token)

    lines = output.getvalue().splitlines()
    assert lines[0] == "test_note_exception:Shop.retry()"
    assert lines[-1] == "test_note_exception:Shop.retry !! KeyError !! KeyError"


def test_printer_lists_notes_after_an_escape() -> None:
    dispatch = binding(Shop, "dispatch")
    output = io.StringIO()

    def note_then_raise_other(self: Any, sku: str) -> str:
        note_exception(KeyError("missing"))
        raise RuntimeError("gave up")

    dispatch.on_call.decorates(_replacing(note_then_raise_other))

    token = _scoped_sinks.set(_scoped_sinks.get() + (Printer(output, timing=False),))
    try:
        with dispatch, pytest.raises(RuntimeError):
            Shop().dispatch("missing")
    finally:
        _scoped_sinks.reset(token)

    assert output.getvalue().splitlines()[-1] == (
        "test_note_exception:Shop.dispatch !! RuntimeError !! KeyError"
    )


# ---------------------------------------------------------------------------
# the framework shape, end to end, with no framework
# ---------------------------------------------------------------------------


def _environ(**overrides: Any) -> dict[str, Any]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": "GET",
        "SCRIPT_NAME": "",
        "PATH_INFO": "/quote/missing",
        "QUERY_STRING": "",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.url_scheme": "http",
        "REMOTE_ADDR": "127.0.0.1",
    }
    environ.update(overrides)
    return environ


class App:
    """A WSGI application that catches its view's exception, hands it
    to handle_exception() and answers 500, as Flask does."""

    def __init__(self) -> None:
        self.pricing = Pricing()

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        try:
            price = self.pricing.quote(environ["PATH_INFO"].rsplit("/", 1)[-1])
        except Exception as exc:
            status, body = self.handle_exception(exc)
        else:
            status, body = "200 OK", str(price).encode()

        start_response(status, [("Content-Type", "text/plain")])
        return [body]

    def handle_exception(self, exc: BaseException) -> tuple[str, bytes]:
        return "500 INTERNAL SERVER ERROR", b"oops"


def test_a_handler_notes_the_exception_against_the_request() -> None:
    def noting(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        current_event(kind="request").note_exception(args[0])
        return wrapped(*args, **kwargs)

    handle = binding(App, "handle_exception").on_call.decorates(noting)
    quote = binding(Pricing, "quote")
    wrapped = WSGIMiddleware(App())

    statuses: list[str] = []

    def start_response(status: str, headers: Any, exc_info: Any = None) -> None:
        statuses.append(status)

    with timeline(handle, quote) as tape:
        body = wrapped(_environ(), start_response)
        chunks = list(body)
        body.close()

    assert chunks == [b"oops"]
    assert statuses == ["500 INTERNAL SERVER ERROR"]

    request = tape.all[0]
    assert request.kind == "request"
    assert request.result == "500 INTERNAL SERVER ERROR"
    assert request.exception is None
    assert request.failed
    assert [type(c.exception) for c in request.caught] == [KeyError]

    # The line says both: the request returned, and it failed.

    assert tape.tree().splitlines()[0] == (
        "GET /quote/missing (test_note_exception:App)  -> '500 INTERNAL SERVER ERROR'"
        "  !! KeyError"
    )
    assert tape.roots()[0] is request
    assert tape.for_binding(handle).first.caught == ()
