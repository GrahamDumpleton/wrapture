"""Tests for injected outcomes on async targets: a stub on a coroutine
function arrives on await, on an async generator function on iteration,
and recording tells an awaited call from one that never was."""

import asyncio
import contextlib
import gc
import inspect
import warnings
from collections.abc import AsyncIterator, Generator
from typing import Any

import pytest
import wrapt

import wrapture
from wrapture import SequenceExhaustedError, binding, timeline


class Client:
    async def fetch(self, page: int) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"page": page}

    async def stream(self, count: int) -> AsyncIterator[int]:
        for index in range(count):
            yield index

    def plain(self, page: int) -> dict[str, Any]:
        return {"page": page}

    @wrapt.mark_as_async
    def looks_async(self, page: int) -> Any:
        async def inner() -> dict[str, Any]:
            return {"page": page}

        return inner()


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def client() -> Any:
    """A Client typed as Any, for calls that deliberately misuse the
    methods (never awaited, a bad keyword, a stubbed plain value)."""

    return Client()


async def drain(iterator: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in iterator]


@contextlib.contextmanager
def _discarding() -> Generator[None]:
    """Close and collect a deliberately un-awaited coroutine without the
    interpreter's warning about it (raised at close on some versions,
    at collection on others) reaching the test output."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield
        gc.collect()


# ---------------------------------------------------------------------------
# coroutine functions
# ---------------------------------------------------------------------------


def test_returns_on_a_coroutine_function_arrives_on_await() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.returns({"page": "stub"})

    with fetch:
        outcome = Client().fetch(1)
        assert inspect.iscoroutine(outcome)
        assert run(outcome) == {"page": "stub"}

    # Unbound again afterwards.

    assert run(Client().fetch(1)) == {"page": 1}


def test_raises_on_a_coroutine_function_arrives_on_await() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.raises(TimeoutError("down"))

    with fetch:
        outcome = Client().fetch(1)  # no raise at the call
        assert inspect.iscoroutine(outcome)

        with pytest.raises(TimeoutError, match="down"):
            run(outcome)


def test_returns_from_on_a_coroutine_function_draws_per_await() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.returns_from(["a", "b"])
    fetch.on_call.then().passes_through()

    async def three() -> list[Any]:
        client = Client()
        return [await client.fetch(1), await client.fetch(2), await client.fetch(3)]

    with fetch:
        assert run(three()) == ["a", "b", {"page": 3}]


def test_exhaustion_with_no_successor_is_reported_at_the_call() -> None:
    # A configuration error, surfaced where it is easiest to see: the
    # call, not a later await.

    fetch = binding(Client, "fetch")
    fetch.on_call.returns_from(["only"])

    async def twice() -> None:
        await client().fetch(1)
        client().fetch(2)

    with fetch:
        with pytest.raises(SequenceExhaustedError):
            run(twice())


def test_result_stages_apply_to_the_awaited_stub() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.returns({"page": 0})
    fetch.on_call.transforms_result(lambda result: {**result, "stubbed": True})

    seen: list[Any] = []
    fetch.on_call.validates_result(seen.append)

    with fetch:
        outcome = Client().fetch(1)
        assert seen == []  # nothing has run yet
        assert run(outcome) == {"page": 0, "stubbed": True}
        assert seen == [{"page": 0}]  # added second, so inside the transform


def test_a_validates_result_failure_arrives_on_await() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.returns({"page": -1})

    def non_negative(result: dict[str, Any]) -> None:
        assert result["page"] >= 0, "negative page"

    fetch.on_call.validates_result(non_negative)

    with fetch:
        outcome = Client().fetch(1)

        with pytest.raises(AssertionError, match="negative page"):
            run(outcome)


def test_argument_stages_still_run_at_the_call() -> None:
    # Argument checks happen before the coroutine exists for the real
    # target too, so a failing one raises at the call.

    fetch = binding(Client, "fetch")
    fetch.on_call.returns({"page": 0})

    def positive(page: int) -> None:
        assert page > 0, "page must be positive"

    fetch.on_call.validates_args(positive)

    with fetch:
        with pytest.raises(AssertionError, match="positive"):
            client().fetch(0)

        assert run(Client().fetch(1)) == {"page": 0}


def test_strict_check_stays_at_the_call() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.returns({"page": 0})

    with fetch:
        with pytest.raises(TypeError, match="stubbed"):
            client().fetch(1, bogus=True)


def test_decorates_owns_its_outcome() -> None:
    # decorates() is not an injecting terminal: what it returns is what
    # the caller gets, awaitable or not.

    fetch = binding(Client, "fetch")
    fetch.on_call.decorates(lambda wrapped, instance, args, kwargs: "plain")

    with fetch:
        assert client().fetch(1) == "plain"

    async def canned(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        return {"page": "decorated"}

    fetch.on_call.decorates(canned)

    with fetch:
        assert run(Client().fetch(1)) == {"page": "decorated"}


def test_an_awaitable_given_to_returns_passes_through() -> None:
    fetch = binding(Client, "fetch")

    async def canned() -> dict[str, Any]:
        return {"page": "own"}

    fetch.on_call.returns(canned())

    with fetch:
        assert run(Client().fetch(1)) == {"page": "own"}


def test_phases_hand_over_on_the_awaited_outcome() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.raises(TimeoutError("down"))
    fetch.on_call.then(after=2).returns({"page": "back"})

    async def retrying() -> list[Any]:
        client = Client()
        outcomes: list[Any] = []
        for _ in range(3):
            try:
                outcomes.append(await client.fetch(1))
            except TimeoutError as exc:
                outcomes.append(str(exc))
        return outcomes

    with fetch:
        assert run(retrying()) == ["down", "down", {"page": "back"}]


def test_until_predicate_sees_the_awaited_stub() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.returns_from([{"page": 1}, {"page": 2}, {"page": 3}])
    fetch.on_call.then(until=lambda event: event.result["page"] == 2).returns("done")

    async def three() -> list[Any]:
        client = Client()
        return [await client.fetch(0) for _ in range(3)]

    with fetch:
        assert run(three()) == [{"page": 1}, {"page": 2}, "done"]


def test_recording_an_awaited_stub() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.returns({"page": "stub"})

    with timeline(fetch) as tape:
        outcome = Client().fetch(1)
        (event,) = tape.all
        assert not event.finished
        assert event.injected

        run(outcome)
        assert event.finished
        assert event.result == {"page": "stub"}
        assert event.duration is not None


def test_a_stacked_binding_sees_through_to_the_coroutine_function() -> None:
    inner = binding(Client, "fetch")
    outer = binding(Client, "fetch")
    outer.on_call.returns({"page": "outer"})

    with inner, outer:
        outcome = Client().fetch(1)
        assert inspect.iscoroutine(outcome)
        assert run(outcome) == {"page": "outer"}


def test_sync_targets_are_unchanged() -> None:
    plain = binding(Client, "plain")
    plain.on_call.returns({"page": "stub"})

    with plain:
        assert Client().plain(1) == {"page": "stub"}

    plain.on_call.raises(ValueError("now"))

    with plain:
        with pytest.raises(ValueError, match="now"):
            Client().plain(1)


def test_the_declared_convention_decides() -> None:
    # A stub has no outcome to inspect, so it follows what introspection
    # reports: wrapt's mark_as_async makes a plain def count as a
    # coroutine function, and its stub arrives on await.

    assert inspect.iscoroutinefunction(vars(Client)["looks_async"])

    looks_async = binding(Client, "looks_async")
    looks_async.on_call.returns({"page": "stub"})

    with looks_async:
        assert run(Client().looks_async(1)) == {"page": "stub"}


# ---------------------------------------------------------------------------
# async generator functions
# ---------------------------------------------------------------------------


def test_returns_on_an_async_generator_function_is_iterated() -> None:
    stream = binding(Client, "stream")
    stream.on_call.returns([10, 20])

    with stream, timeline() as tape:
        outcome = Client().stream(5)
        assert inspect.isasyncgen(outcome)
        assert run(drain(outcome)) == [10, 20]

        (event,) = tape.all
        assert event.items == 2
        assert event.finished

    assert run(drain(Client().stream(2))) == [0, 1]


def test_an_async_iterable_given_to_returns_passes_through() -> None:
    stream = binding(Client, "stream")

    async def own() -> AsyncIterator[str]:
        yield "own"

    stream.on_call.returns(own())

    with stream:
        assert run(drain(Client().stream(5))) == ["own"]


def test_raises_on_an_async_generator_function_arrives_on_iteration() -> None:
    stream = binding(Client, "stream")
    stream.on_call.raises(RuntimeError("closed"))

    with stream:
        outcome = Client().stream(5)  # no raise at the call
        assert inspect.isasyncgen(outcome)

        with pytest.raises(RuntimeError, match="closed"):
            run(drain(outcome))


def test_returns_from_on_an_async_generator_function() -> None:
    stream = binding(Client, "stream")
    stream.on_call.returns_from([[1], [2, 3]])
    stream.on_call.then().passes_through()

    async def three() -> list[list[int]]:
        client = Client()
        return [await drain(client.stream(1)) for _ in range(3)]

    with stream:
        assert run(three()) == [[1], [2, 3], [0]]


# ---------------------------------------------------------------------------
# finished and pending
# ---------------------------------------------------------------------------


def test_a_never_awaited_call_stays_pending() -> None:
    fetch = binding(Client, "fetch")

    with timeline(fetch) as tape:
        client = Client()
        run(client.fetch(1))
        forgotten = client.fetch(2)  # created, never awaited

        fetch.events.assert_times(2)
        fetch.events.finished().assert_once()
        fetch.events.pending().assert_once()
        assert fetch.events.pending().first.arguments == {"page": 2}

    assert tape.pending == 1
    assert repr(tape) == "<Tape: 2 events, 1 pending>"

    # The recording wrapper is named after the target, so Python's own
    # "never awaited" warning points at the call, not at wrapture.

    assert forgotten.__qualname__ == "Client.fetch"

    with _discarding():
        forgotten.close()
        del forgotten


def test_awaiting_after_the_scope_clears_pending() -> None:
    fetch = binding(Client, "fetch")

    with timeline(fetch) as tape:
        late = Client().fetch(1)

    assert tape.pending == 1
    assert run(late) == {"page": 1}
    assert tape.pending == 0
    assert tape.for_binding(fetch).finished().count == 1


def test_an_unfinished_generator_is_pending_until_closed() -> None:
    stream = binding(Client, "stream")

    with timeline(stream) as tape:

        async def partial() -> list[int]:
            iterator = client().stream(3)
            first = await iterator.__anext__()
            pending_after_one = tape.for_binding(stream).pending().count

            await iterator.aclose()
            pending_after_close = tape.for_binding(stream).pending().count
            return [first, pending_after_one, pending_after_close]

        assert run(partial()) == [0, 1, 0]
        assert tape.for_binding(stream).finished().count == 1


def test_finished_and_pending_chain_with_other_filters() -> None:
    fetch = binding(Client, "fetch")
    fetch.on_call.returns({"page": "stub"})

    with timeline(fetch):
        client = Client()
        run(client.fetch(1))
        pending = client.fetch(2)

        fetch.events.with_args(page=1).finished().assert_once()
        fetch.events.injected().pending().assert_once()
        assert fetch.events.finished().label.endswith("[finished]")
        assert fetch.events.pending().label.endswith("[pending]")

        # A stub coroutine is named after the target, so Python's own
        # "coroutine ... was never awaited" warning names the stubbed
        # call rather than wrapture's helper.

        assert pending.__qualname__ == "Client.fetch"
        assert pending.__name__ == "fetch"

        with _discarding():
            pending.close()
            del pending


def test_wrapture_exports_nothing_new() -> None:
    assert not hasattr(wrapture, "pending")
