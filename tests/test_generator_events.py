"""Tests for recording calls that produce generators.

One event covers the whole iteration: it opens when the call creates
the generator, counts items as they are yielded, nests body work under
itself per resumption, and closes at exhaustion with the generator's
return value, or with no outcome at all when the iteration was
abandoned, leaving the event visibly unfinished.
"""

import asyncio
import gc
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from wrapt import MISSING

from wrapture import binding, timeline


class Feed:
    def fetch(self, page: int) -> list[str]:
        return [f"item_{page}_{n}" for n in range(2)]

    def stream(self, pages: int) -> Generator[str, Any, str]:
        for page in range(pages):
            yield from self.fetch(page)
        return f"streamed {pages} pages"

    def echo(self) -> Generator[Any, Any, str]:
        received: list[str] = []
        while True:
            value = yield received[-1] if received else "ready"
            if value is None:
                return ",".join(received)
            received.append(value)


class Consumer:
    def handle(self, item: str) -> str:
        return item.upper()


# ---------------------------------------------------------------------------
# exhaustion
# ---------------------------------------------------------------------------


def test_one_event_covers_the_whole_iteration() -> None:
    stream = binding(Feed, "stream")

    with timeline(stream) as tape:
        produced = list(Feed().stream(2))

    (event,) = tape.all

    assert produced == ["item_0_0", "item_0_1", "item_1_0", "item_1_1"]
    assert event.kind == "call"
    assert event.items == 4
    assert event.result == "streamed 2 pages"


def test_a_plain_generator_records_result_none_at_exhaustion() -> None:
    class Holder:
        @staticmethod
        def gen() -> Generator[int, Any, None]:
            yield 1

    gen = binding(Holder, "gen")

    with timeline(gen) as tape:
        list(Holder.gen())

    # None distinguishes a finished iteration from an abandoned one,
    # whose result stays MISSING.

    assert tape.all[0].result is None


def test_durations_record_wall_and_body_separately() -> None:
    stream = binding(Feed, "stream")

    with timeline(stream) as tape:
        for _ in Feed().stream(2):
            pass

    (event,) = tape.all

    assert event.duration is not None
    assert event.body_duration is not None
    assert 0 < event.body_duration <= event.duration


def test_the_item_count_is_live_while_iteration_is_in_progress() -> None:
    stream = binding(Feed, "stream")

    with timeline(stream) as tape:
        wrapped = Feed().stream(2)
        next(wrapped)
        next(wrapped)

        (event,) = tape.all
        assert event.items == 2
        assert event.result is MISSING
        assert event.duration is None

        wrapped.close()


# ---------------------------------------------------------------------------
# nesting per resumption
# ---------------------------------------------------------------------------


def test_body_calls_nest_under_the_generator_event() -> None:
    stream = binding(Feed, "stream")
    fetch = binding(Feed, "fetch")
    handle = binding(Consumer, "handle")

    with timeline(stream, fetch, handle) as tape:
        consumer = Consumer()
        for item in Feed().stream(1):
            consumer.handle(item)

    generator_event = tape.all[0]
    fetch_events = [e for e in tape.all if e.label == "Feed.fetch"]
    handle_events = [e for e in tape.all if e.label == "Consumer.handle"]

    # fetch() runs inside the generator body, so it nests under the
    # generator's event. handle() runs in the consumer between yields,
    # so it does not.

    assert [e.parent_id for e in fetch_events] == [generator_event.seq]
    assert [e.parent_id for e in handle_events] == [None, None]
    assert tape.children_of(generator_event) == fetch_events


# ---------------------------------------------------------------------------
# the generator protocol survives recording
# ---------------------------------------------------------------------------


def test_send_and_the_return_value_are_forwarded() -> None:
    echo = binding(Feed, "echo")

    with timeline(echo) as tape:
        gen = Feed().echo()
        assert next(gen) == "ready"
        assert gen.send("a") == "a"
        assert gen.send("b") == "b"

        with pytest.raises(StopIteration) as stop:
            gen.send(None)
        assert stop.value.value == "a,b"

    assert tape.all[0].result == "a,b"


def test_an_unhandled_throw_records_the_exception() -> None:
    stream = binding(Feed, "stream")

    with timeline(stream) as tape:
        gen = Feed().stream(2)
        next(gen)

        with pytest.raises(TimeoutError):
            gen.throw(TimeoutError("consumer gave up"))

    (event,) = tape.all
    assert isinstance(event.exception, TimeoutError)
    assert event.duration is not None


def test_an_exception_in_the_body_records_and_propagates() -> None:
    class Holder:
        @staticmethod
        def gen() -> Generator[int, Any, None]:
            yield 1
            raise ValueError("bad row")

    gen = binding(Holder, "gen")

    with timeline(gen) as tape:
        with pytest.raises(ValueError, match="bad row"):
            list(Holder.gen())

    (event,) = tape.all
    assert isinstance(event.exception, ValueError)
    assert event.items == 1


# ---------------------------------------------------------------------------
# abandonment
# ---------------------------------------------------------------------------


def test_close_leaves_the_event_visibly_unfinished() -> None:
    stream = binding(Feed, "stream")

    with timeline(stream) as tape:
        gen = Feed().stream(3)
        next(gen)
        gen.close()

    (event,) = tape.all

    # Closed but with no outcome: durations and the item count are
    # recorded, the result is not, so the tape shows an iteration that
    # never finished.

    assert event.items == 1
    assert event.duration is not None
    assert event.result is MISSING
    assert event.exception is None


def test_a_dropped_generator_is_recorded_as_abandoned() -> None:
    stream = binding(Feed, "stream")

    with timeline(stream) as tape:
        gen = Feed().stream(3)
        next(gen)
        del gen
        gc.collect()

    (event,) = tape.all
    assert event.result is MISSING
    assert event.duration is not None


# ---------------------------------------------------------------------------
# async generators
# ---------------------------------------------------------------------------


class AsyncFeed:
    async def stream(self, pages: int) -> AsyncGenerator[str, Any]:
        for page in range(pages):
            await asyncio.sleep(0)
            yield f"item_{page}"


def test_an_async_generator_records_items_and_completion() -> None:
    stream = binding(AsyncFeed, "stream")

    async def consume() -> list[str]:
        return [item async for item in AsyncFeed().stream(3)]

    with timeline(stream) as tape:
        produced = asyncio.run(consume())

    (event,) = tape.all

    assert produced == ["item_0", "item_1", "item_2"]
    assert event.items == 3
    assert event.result is None
    assert event.body_duration is not None


def test_an_abandoned_async_generator_stays_unfinished() -> None:
    stream = binding(AsyncFeed, "stream")

    async def partial() -> None:
        gen = AsyncFeed().stream(3)
        await anext(gen)
        await gen.aclose()

    with timeline(stream) as tape:
        asyncio.run(partial())

    (event,) = tape.all
    assert event.items == 1
    assert event.result is MISSING
    assert event.duration is not None


# ---------------------------------------------------------------------------
# decorators that hide or remove generator-ness
# ---------------------------------------------------------------------------


def test_a_hidden_generator_function_still_records_iteration() -> None:
    import functools
    import inspect

    def logged(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return wrapper

    class Hidden:
        @logged
        def stream(self, n: int) -> Generator[int, Any, str]:
            yield from range(n)
            return "done"

    # Introspection reports a plain function; the call still returns a
    # generator, which is all the recording dispatch looks at.

    assert not inspect.isgeneratorfunction(vars(Hidden)["stream"])

    stream = binding(Hidden, "stream")

    with timeline(stream) as tape:
        assert list(Hidden().stream(3)) == [0, 1, 2]

    (event,) = tape.all
    assert event.items == 3
    assert event.result == "done"


def test_a_hidden_async_generator_still_records_iteration() -> None:
    import functools
    import inspect

    def logged(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return wrapper

    class Hidden:
        @logged
        def stream(self, n: int) -> Any:
            async def inner() -> AsyncGenerator[int, Any]:
                for i in range(n):
                    await asyncio.sleep(0)
                    yield i

            return inner()

    assert not inspect.isasyncgenfunction(vars(Hidden)["stream"])

    stream = binding(Hidden, "stream")

    async def consume() -> list[int]:
        return [item async for item in Hidden().stream(3)]

    with timeline(stream) as tape:
        assert asyncio.run(consume()) == [0, 1, 2]

    (event,) = tape.all
    assert event.items == 3
    assert event.result is None


def test_a_decorator_that_materializes_the_iteration_records_a_plain_result() -> None:
    import functools
    import inspect

    def eager(fn: Any) -> Any:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return list(fn(*args, **kwargs))

        return wrapper

    class Eager:
        @eager
        def stream(self, n: int) -> Any:
            yield from range(n)

    # The opposite lie: introspection says generator function, but the
    # call returns a list, so it records as an ordinary result with no
    # iteration tracking.

    assert inspect.isgeneratorfunction(vars(Eager)["stream"].__wrapped__)

    stream = binding(Eager, "stream")

    with timeline(stream) as tape:
        assert Eager().stream(3) == [0, 1, 2]

    (event,) = tape.all
    assert event.result == [0, 1, 2]

    # No iteration tracking: no item count, no body time. The plain
    # call duration every event now carries is unrelated to iteration.

    assert event.items is None
    assert event.body_duration is None
    assert event.duration is not None


# ---------------------------------------------------------------------------
# composition with the iterator() proxy
# ---------------------------------------------------------------------------


def test_recording_wraps_outside_a_behaviour_applied_iterator_proxy() -> None:
    from wrapture import iterator

    seen: list[str] = []
    shout = iterator()
    shout.on_item.transforms_item(str.upper)
    shout.on_abandon.notifies(lambda: seen.append("abandoned"))

    def wrap(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        return shout(wrapped(*args, **kwargs))

    stream = binding(Feed, "stream").on_call.decorates(wrap)

    # The recording relay wraps whatever behaviour produced, so the
    # consumer sees proxied items while the binding still records the
    # iteration: count, return value, and abandonment all thread
    # through both levels.

    with timeline(stream) as tape:
        assert list(Feed().stream(1)) == ["ITEM_0_0", "ITEM_0_1"]

        gen = Feed().stream(3)
        next(gen)
        gen.close()

    finished, abandoned = tape.all

    assert finished.items == 2
    assert finished.result == "streamed 1 pages"
    assert abandoned.items == 1
    assert abandoned.result is MISSING
    assert seen == ["abandoned"]


# ---------------------------------------------------------------------------
# outside a timeline nothing is wrapped
# ---------------------------------------------------------------------------


def test_without_a_timeline_the_generator_is_returned_unwrapped() -> None:
    stream = binding(Feed, "stream")

    with stream:
        gen = Feed().stream(1)

        assert getattr(gen, "__name__", None) == "stream"
        assert list(gen) == ["item_0_0", "item_0_1"]
