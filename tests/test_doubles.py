"""Tests for stub(): the supplied stand-in callable.

A stub is a one-callable stand-in the test places itself: permissive by
default (any arguments, returns None, records), with returns=/raises=
to dictate the outcome, kind= to make the stand-in a generator,
coroutine or async generator for real, and mimics= to borrow a
callable's signature and kind, opting back into strict checking and
by-name argument recording.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import warnings
import weakref
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest

from wrapture import ObservedCallable, stub, timeline


@contextmanager
def _discarding() -> Generator[None]:
    # Swallow the never-awaited RuntimeWarning for tests that create a
    # coroutine deliberately and drop it.

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield
        gc.collect()


# ---------------------------------------------------------------------------
# the bare stub
# ---------------------------------------------------------------------------


def test_a_bare_stub_accepts_anything_returns_none_and_records() -> None:
    hook = stub()

    with timeline():
        assert hook() is None
        assert hook(1, 2, three=3) is None

        hook.events.assert_times(2)


def test_the_default_label_is_stub_and_a_given_label_names_events() -> None:
    plain = stub()
    named = stub("before_start")

    assert plain.label == "stub"
    assert named.label == "before_start"
    assert named.path == "stub:before_start"

    with timeline():
        named(1)
        assert named.events.first.label == "before_start"


def test_the_stub_is_an_observed_callable() -> None:
    hook = stub()

    assert isinstance(hook, ObservedCallable)
    assert weakref.ref(hook)() is hook


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


def test_returns_makes_every_call_produce_the_value() -> None:
    charge = stub("charge", returns={"id": "stub"})

    with timeline():
        assert charge(500) == {"id": "stub"}
        assert charge() == {"id": "stub"}

        charge.events.returning({"id": "stub"}).assert_times(2)


def test_raises_makes_every_call_raise() -> None:
    charge = stub("charge", raises=TimeoutError("down"))

    with timeline():
        with pytest.raises(TimeoutError, match="down"):
            charge(500)

        charge.events.raising(TimeoutError).assert_once()


def test_raises_accepts_an_exception_class() -> None:
    charge = stub(raises=TimeoutError)

    with timeline():
        with pytest.raises(TimeoutError):
            charge()


def test_returns_and_raises_together_are_refused() -> None:
    with pytest.raises(ValueError, match="not both"):
        stub(returns=1, raises=KeyError())


def test_raises_must_be_an_exception() -> None:
    with pytest.raises(TypeError, match="exception instance or class"):
        stub(raises="nope")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# mimics=: strictness and by-name recording
# ---------------------------------------------------------------------------


class Task:
    def before_start(self, task_id: str, args: tuple[Any, ...]) -> None: ...


class Client:
    async def fetch(self, url: str, timeout: float = 5.0) -> Any: ...

    async def stream(self, count: int) -> Any:
        yield count

    def paginate(self, size: int) -> Any:
        yield size


def test_mimics_records_arguments_by_parameter_name() -> None:
    hook = stub(mimics=Task.before_start)

    class Generated:
        before_start: Any = hook

    task = Generated()
    with timeline():
        task.before_start("id-1", (2, 2))

        event = hook.events.with_args(task_id="id-1").assert_once().first
        assert event.instance is task
        assert event.arguments == {"task_id": "id-1", "args": (2, 2)}


def test_mimics_checks_calls_and_a_rejected_call_leaves_no_trace() -> None:
    hook = stub(mimics=Task.before_start)

    class Generated:
        before_start: Any = hook

    task = Generated()
    with timeline():
        task.before_start("id-1", (2, 2))

        with pytest.raises(TypeError, match=r"\(stubbed\): missing a required"):
            task.before_start("id-1")
        with pytest.raises(TypeError, match=r"\(stubbed\): got an unexpected"):
            task.before_start("id-1", (2, 2), bogus=True)

        hook.events.assert_once()


def test_mimics_reports_the_borrowed_signature() -> None:
    hook = stub(mimics=Task.before_start)

    class Generated:
        before_start: Any = hook

    bound = inspect.signature(Generated().before_start)
    assert list(bound.parameters) == ["task_id", "args"]


def test_mimics_takes_the_label_from_the_callable() -> None:
    hook = stub(mimics=Task.before_start)

    assert hook.label.endswith("Task.before_start")
    assert hook.__name__ == "before_start"


def test_mimics_and_kind_together_are_refused() -> None:
    with pytest.raises(TypeError, match="kind is inferred from mimics"):
        stub(mimics=Task.before_start, kind="coroutine")


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        stub(kind="agenda")


# ---------------------------------------------------------------------------
# async kinds
# ---------------------------------------------------------------------------


def test_kind_coroutine_resolves_on_await() -> None:
    fetch = stub("fetch", kind="coroutine", returns={"ok": True})

    assert inspect.iscoroutinefunction(fetch)

    async def run() -> None:
        with timeline():
            assert await fetch("http://x") == {"ok": True}

            fetch.events.finished().assert_once()
            fetch.events.pending().assert_never()

    asyncio.run(run())


def test_kind_coroutine_raises_on_await() -> None:
    fetch = stub("fetch", kind="coroutine", raises=TimeoutError("down"))

    async def run() -> None:
        with timeline():
            with pytest.raises(TimeoutError):
                await fetch("http://x")

            fetch.events.raising(TimeoutError).assert_once()

    asyncio.run(run())


def test_a_called_but_never_awaited_stub_stays_pending() -> None:
    fetch = stub("fetch", kind="coroutine")

    async def run() -> None:
        with timeline(), _discarding():
            fetch("http://x")

            fetch.events.assert_once()
            fetch.events.pending().assert_once()
            fetch.events.finished().assert_never()

    asyncio.run(run())


def test_the_delivered_coroutine_is_named_for_the_mimicked_target() -> None:
    fetch = stub(mimics=Client.fetch)

    with timeline(), _discarding():
        delivery = fetch(None, "http://x")
        assert delivery.__qualname__ == "Client.fetch"
        delivery.close()


def test_kind_inference_from_mimics() -> None:
    assert inspect.iscoroutinefunction(stub(mimics=Client.fetch))
    assert inspect.isasyncgenfunction(stub(mimics=Client.stream))
    assert inspect.isgeneratorfunction(stub(mimics=Client.paginate))
    assert not inspect.iscoroutinefunction(stub(mimics=Task.before_start))


def test_kind_async_generator_yields_the_returns_items() -> None:
    stream = stub("stream", kind="async_generator", returns=[1, 2])

    async def run() -> None:
        with timeline():
            assert [item async for item in stream()] == [1, 2]

            stream.events.finished().assert_once()

    asyncio.run(run())


def test_kind_async_generator_raises_fails_the_iteration() -> None:
    stream = stub("stream", kind="async_generator", raises=TimeoutError("down"))

    async def run() -> None:
        with timeline():
            iterator = stream()
            with pytest.raises(TimeoutError):
                await anext(iterator)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# the sync generator kind
# ---------------------------------------------------------------------------


def test_kind_generator_yields_the_returns_items() -> None:
    items = stub("items", kind="generator", returns=[1, 2, 3])

    with timeline():
        produced = items()
        assert list(produced) == [1, 2, 3]

        items.events.finished().assert_once()


def test_kind_generator_raises_fails_the_iteration() -> None:
    items = stub("items", kind="generator", raises=KeyError("gone"))

    with timeline():
        produced = items()

        with pytest.raises(KeyError):
            next(produced)


def test_generator_kinds_require_an_iterable_returns() -> None:
    with pytest.raises(TypeError, match="iterable of items"):
        stub(kind="generator", returns=42)
    with pytest.raises(TypeError, match="iterable of items"):
        stub(kind="async_generator", returns=42)


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------


def test_placed_on_a_class_the_stub_records_the_instance() -> None:
    hook = stub("on_success")

    class Generated:
        on_success: Any = hook

    first = Generated()
    second = Generated()
    with timeline():
        first.on_success(4, "id-1")
        second.on_success(9, "id-2")

        hook.events.with_instance(first).assert_once()
        hook.events.with_instance(second).assert_once()


def test_passed_as_a_plain_callback() -> None:
    on_ready = stub("on_ready")

    def fire(callback: Any) -> None:
        callback("ready")

    with timeline():
        fire(on_ready)

        on_ready.events.assert_once()


def test_suspend_and_resume_control_recording_not_the_outcome() -> None:
    charge = stub("charge", returns=1)

    with timeline():
        charge.suspend()
        assert charge() == 1
        charge.resume()
        assert charge() == 1

        charge.events.assert_once()
        assert charge.suspended_calls == 1
