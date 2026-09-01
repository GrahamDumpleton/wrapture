"""Tests for leaf bindings: binding(..., leaf=True) and its siblings.

A leaf records its own event and silences everything that would make
a span beneath it for its whole extent: nested bindings, attribute
accesses, blocks and nested requests, through a generator's
iteration, a coroutine's await and a streamed body, and into threads
handed the context. Log captures are the exception: a log record is
an instantaneous event on whatever is in flight, so beneath a leaf it
records attached to the leaf. The silence is structural, declared
with the binding, and counts nothing: the leaf on the tape is the
explanation for its missing children.
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
    WSGIMiddleware,
    binding,
    capture_logs,
    detach,
    load_config,
    observed,
    propagate,
    timeline,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class Gateway:
    def charge(self, amount: int) -> str:
        wrapture.annotate(vendor="acme")
        return f"ch_{amount}"


class Model:
    status = "draft"


class Service:
    def __init__(self) -> None:
        self.gateway = Gateway()

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
    gateway.charge(1)
    start_response("200 OK", [("Content-Type", "text/plain")])

    def body() -> Generator[bytes, None, None]:
        gateway.charge(2)
        yield b"hello "
        gateway.charge(3)
        yield b"world"

    return body()


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


def _serve(app: Any, environ: dict[str, Any]) -> bytes:
    iterable = app(environ, lambda *a: None)
    try:
        return b"".join(iterable)
    finally:
        close = getattr(iterable, "close", None)
        if close is not None:
            close()


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
# the reach of the silence
# ---------------------------------------------------------------------------


def test_a_leaf_records_itself_and_nothing_beneath_it() -> None:
    place = binding(Service, "place", leaf=True)
    charge = binding(Gateway, "charge")

    with place, charge, timeline() as tape:
        assert Service().place(5) == "ch_5"

    (event,) = tape.all
    assert event.binding is place
    assert event.finished
    assert place.leaf is True
    assert charge.leaf is False

    # Structural, not counted: nothing is filtered, suspended or missed.

    assert charge.filtered_calls == 0
    assert charge.missed_calls == 0


def test_annotations_from_beneath_a_leaf_land_on_the_leaf() -> None:
    # Nothing beneath the leaf pushed, so the leaf is the innermost
    # in-flight event and annotate() inside its body reaches it.

    place = binding(Service, "place", leaf=True)
    charge = binding(Gateway, "charge")

    with place, charge, timeline() as tape:
        Service().place(5)

    (event,) = tape.all
    assert event.data["vendor"] == "acme"


def test_logs_beneath_a_leaf_record_attached_to_it() -> None:
    log = logging.getLogger("leaf.inner")
    log.setLevel(logging.DEBUG)

    def work() -> None:
        log.warning("inside the leaf")
        gateway.charge(1)

    silenced = observed(work, leaf=True)
    charge = binding(Gateway, "charge")
    logs = capture_logs("leaf.*")

    with charge, logs, timeline() as tape:
        silenced()

    assert _kinds(tape) == ["call", "log"]
    leaf, record = tape.all
    assert record.parent_id == leaf.seq
    assert record.data["message"] == "inside the leaf"


def test_blocks_and_attribute_accesses_beneath_a_leaf_are_silenced() -> None:
    def work() -> None:
        model = Model()
        model.status = "published"
        with wrapture.block("render"):
            gateway.charge(1)

    silenced = observed(work, leaf=True)
    status = binding(Model, "status")
    charge = binding(Gateway, "charge")

    with status, charge, timeline() as tape:
        silenced()

    assert _kinds(tape) == ["call"]
    assert status.filtered_calls == 0


def test_the_silence_covers_a_generators_iteration() -> None:
    stream = binding(Service, "stream", leaf=True)
    charge = binding(Gateway, "charge")

    with stream, charge, timeline() as tape:
        assert list(Service().stream(3)) == ["ch_0", "ch_1", "ch_2"]

    (event,) = tape.all
    assert event.items == 3
    assert charge.filtered_calls == 0


def test_the_silence_covers_a_coroutines_await() -> None:
    fetch = binding(Service, "fetch", leaf=True)
    charge = binding(Gateway, "charge")

    with fetch, charge, timeline() as tape:
        assert asyncio.run(Service().fetch(4)) == "ch_4"

    (event,) = tape.all
    assert event.result == "ch_4"


def test_the_silence_follows_the_context_into_threads() -> None:
    in_thread = binding(Service, "in_thread", leaf=True)
    detached = binding(Service, "detached", leaf=True)
    charge = binding(Gateway, "charge")

    with in_thread, detached, charge, timeline() as tape:
        Service().in_thread(3)
        Service().detached(4)

    assert _kinds(tape) == ["call", "call"]
    assert charge.filtered_calls == 0
    assert charge.missed_calls == 0


def test_a_leaf_request_silences_the_application_and_its_streaming_body() -> None:
    app = binding(__name__, "application", mode="wsgi", leaf=True)
    charge = binding(Gateway, "charge")

    with app, charge, timeline() as tape:
        assert _serve(sys.modules[__name__].application, _environ()) == b"hello world"

    (event,) = tape.all
    assert event.kind == "request"
    assert event.items == 2


def test_the_standalone_middlewares_take_leaf() -> None:
    wsgi = WSGIMiddleware(application, leaf=True)
    asgi = ASGIMiddleware(asgi_application, leaf=True)
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        _serve(wsgi, _environ())
        asyncio.run(_serve_asgi(asgi, _scope()))

    assert _kinds(tape) == ["request", "request"]


def test_a_leaf_block_silences_its_body() -> None:
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        with wrapture.block("call-billing", leaf=True):
            gateway.charge(1)
        gateway.charge(2)

    assert _kinds(tape) == ["block", "call"]
    assert tape.all[1].args == (2,)


def test_an_attribute_binding_with_leaf_silences_what_the_access_triggers() -> None:
    class Lazy:
        @property
        def total(self) -> str:
            return gateway.charge(1)

    total = binding(Lazy, "total", leaf=True)
    charge = binding(Gateway, "charge")

    with total, charge, timeline() as tape:
        assert Lazy().total == "ch_1"

    assert _kinds(tape) == ["get"]


# ---------------------------------------------------------------------------
# composition with tree= and with other leaves
# ---------------------------------------------------------------------------


def test_a_leaf_beneath_a_tree_decline_stays_fully_silent() -> None:
    # The decline's silence is the stronger level and is never lowered
    # by the leaf: the leaf records nothing, and the calls beneath it
    # still count as filtered, since the decline left nothing on the
    # tape to explain them.

    outer = observed(lambda: Service().place(1), when=lambda *a: False, tree=True)
    place = binding(Service, "place", leaf=True)
    charge = binding(Gateway, "charge")

    with place, charge, timeline() as tape:
        outer()

    assert tape.all == []
    assert place.filtered_calls == 1
    assert charge.filtered_calls == 1


def test_leaf_and_tree_compose_on_one_binding() -> None:
    place = binding(
        Service,
        "place",
        when=lambda instance, args, kwargs: args[0] > 10,
        tree=True,
        leaf=True,
    )
    charge = binding(Gateway, "charge")

    with place, charge, timeline() as tape:
        Service().place(5)
        Service().place(50)

    (event,) = tape.all
    assert event.args == (50,)
    assert place.filtered_calls == 1
    assert charge.filtered_calls == 1


def test_a_leaf_beneath_a_leaf_is_silenced_like_anything_else() -> None:
    place = binding(Service, "place", leaf=True)
    charge = binding(Gateway, "charge", leaf=True)

    with place, charge, timeline() as tape:
        Service().place(5)

    (event,) = tape.all
    assert event.binding is place


# ---------------------------------------------------------------------------
# declaration surfaces and refusals
# ---------------------------------------------------------------------------


def test_observed_and_bound_take_leaf() -> None:
    charge = binding(Gateway, "charge")

    @observed(leaf=True)
    def order(amount: int) -> str:
        return gateway.charge(amount)

    with charge, timeline() as tape:
        order(5)

    assert _kinds(tape) == ["call"]
    assert order.leaf is True

    @wrapture.bound(Service, "place", leaf=True)
    def run(place: Any) -> None:
        with charge, timeline() as inner:
            Service().place(1)
        assert _kinds(inner) == ["call"]
        assert place.leaf is True

    run()


def test_leaf_must_be_a_boolean() -> None:
    with pytest.raises(TypeError, match="leaf must be True or False"):
        binding(Gateway, "charge", leaf="yes")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="leaf must be True or False"):
        wrapture.block("x", leaf=1)  # type: ignore[arg-type]


def test_leaf_is_refused_on_a_value_binding() -> None:
    with pytest.raises(ValueError, match="leaf= and category= do not apply"):
        binding(Model, attr="status", leaf=True)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_an_observe_entry_takes_leaf() -> None:
    entry = ObserveEntry(target=f"{__name__}:Service", name="place", leaf=True)
    charge = binding(Gateway, "charge")

    applied = Config(observe=[entry]).apply()
    try:
        with charge, timeline() as tape:
            Service().place(5)
    finally:
        applied.revert()

    assert _kinds(tape) == ["call"]


def test_the_loader_accepts_and_validates_leaf(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[observe]]
            target = "{__name__}:Service"
            name = "place"
            leaf = true
            """
        )
    )

    assert load_config(source).observe[0].leaf is True

    with pytest.raises(ConfigError, match="leaf must be true or false"):
        ObserveEntry(target=f"{__name__}:Service", name="place", leaf="yes")  # type: ignore[arg-type]
