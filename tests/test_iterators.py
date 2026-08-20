"""Tests for the iterator() factory and its wrapped iterators."""

import asyncio
import inspect
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest

from wrapture import binding, iterator


def numbers() -> Generator[int, None, None]:
    yield 1
    yield 2
    yield 3


# ---------------------------------------------------------------------------
# item behaviour
# ---------------------------------------------------------------------------


def test_transforms_item_applies_to_each_item() -> None:
    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    assert list(doubles(numbers())) == [2, 4, 6]


def test_validates_item_observes_without_changing_items() -> None:
    seen: list[int] = []
    watched = iterator()
    watched.on_item.validates_item(seen.append)

    assert list(watched(numbers())) == [1, 2, 3]
    assert seen == [1, 2, 3]


def test_validates_item_can_reject() -> None:
    def positive(item: int) -> None:
        assert item > 0, f"non-positive: {item}"

    checked = iterator()
    checked.on_item.validates_item(positive)

    def values() -> Generator[int, None, None]:
        yield 1
        yield -1

    wrapped = checked(values())
    assert next(wrapped) == 1
    with pytest.raises(AssertionError, match="non-positive"):
        next(wrapped)


def test_stages_apply_in_the_order_added() -> None:
    staged = iterator()
    staged.on_item.transforms_item(lambda item: item * 2)
    staged.on_item.transforms_item(lambda item: item + 1)

    assert list(staged(numbers())) == [3, 5, 7]


def test_configuration_chains_from_the_factory() -> None:
    negated = iterator().on_item.transforms_item(lambda item: -item)

    assert list(negated(numbers())) == [-1, -2, -3]


def test_passes_through_clears_item_behaviour() -> None:
    cleared = iterator()
    cleared.on_item.transforms_item(lambda item: 2 * item)
    cleared.on_item.passes_through()

    source = numbers()
    assert cleared(source) is source  # unconfigured: returned unwrapped


def test_unconfigured_factory_returns_the_iterator_unwrapped() -> None:
    plain = iterator()
    source = numbers()

    assert plain(source) is source


def test_behaviour_is_snapshotted_when_the_factory_is_applied() -> None:
    snapshot = iterator()
    snapshot.on_item.transforms_item(lambda item: item * 2)

    wrapped = snapshot(numbers())

    # Reconfiguring the factory must not affect the iterator already
    # wrapped, only ones wrapped afterwards.

    snapshot.on_item.transforms_item(lambda item: item + 1)

    assert list(wrapped) == [2, 4, 6]
    assert list(snapshot(numbers())) == [3, 5, 7]


# ---------------------------------------------------------------------------
# finish, error and abandon behaviour
# ---------------------------------------------------------------------------


def test_finish_receives_the_generator_return_value() -> None:
    def totalling() -> Generator[int, None, str]:
        yield 1
        yield 2
        return "total=3"

    finished: list[str] = []
    watched = iterator()
    watched.on_finish.validates(finished.append)

    assert list(watched(totalling())) == [1, 2]
    assert finished == ["total=3"]


def test_finish_receives_none_for_a_plain_iterator_and_runs_once() -> None:
    finished: list[Any] = []
    watched = iterator()
    watched.on_finish.validates(finished.append)

    wrapped = watched(iter([1]))
    assert list(wrapped) == [1]
    with pytest.raises(StopIteration):
        next(wrapped)

    assert finished == [None]  # once, despite the second exhaustion


def test_finish_check_can_reject_completion() -> None:
    def short() -> Generator[int, None, int]:
        yield 1
        return 0

    def nonzero(value: int) -> None:
        assert value != 0, "empty total"

    checked = iterator()
    checked.on_finish.validates(nonzero)

    wrapped = checked(short())
    assert next(wrapped) == 1
    with pytest.raises(AssertionError, match="empty total"):
        next(wrapped)


def test_error_hook_sees_a_body_exception() -> None:
    def failing() -> Generator[int, None, None]:
        yield 1
        raise RuntimeError("boom")

    errors: list[BaseException] = []
    watched = iterator()
    watched.on_error.notifies(errors.append)

    wrapped = watched(failing())
    assert next(wrapped) == 1
    with pytest.raises(RuntimeError, match="boom"):
        next(wrapped)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_error_hook_sees_an_item_stage_exception() -> None:
    def broken(item: int) -> None:
        raise ValueError("bad")

    errors: list[BaseException] = []
    checked = iterator()
    checked.on_item.validates_item(broken)
    checked.on_error.notifies(errors.append)

    with pytest.raises(ValueError, match="bad"):
        next(checked(numbers()))

    assert len(errors) == 1


def test_error_hook_sees_an_unhandled_thrown_exception() -> None:
    errors: list[BaseException] = []
    watched = iterator()
    watched.on_error.notifies(errors.append)

    wrapped = watched(numbers())
    assert next(wrapped) == 1
    with pytest.raises(ValueError):
        wrapped.throw(ValueError("injected"))

    assert len(errors) == 1


def test_abandon_hook_fires_on_close_before_exhaustion() -> None:
    abandoned: list[bool] = []
    watched = iterator()
    watched.on_abandon.notifies(lambda: abandoned.append(True))

    wrapped = watched(numbers())
    assert next(wrapped) == 1
    wrapped.close()

    assert abandoned == [True]


def test_abandon_hook_does_not_fire_on_exhaustion() -> None:
    abandoned: list[bool] = []
    watched = iterator()
    watched.on_abandon.notifies(lambda: abandoned.append(True))

    wrapped = watched(numbers())
    assert list(wrapped) == [1, 2, 3]
    wrapped.close()  # closing a finished iteration is not abandonment

    assert abandoned == []


def test_abandon_hook_fires_for_an_async_generator() -> None:
    abandoned: list[bool] = []
    watched = iterator()
    watched.on_abandon.notifies(lambda: abandoned.append(True))

    async def stream() -> AsyncGenerator[int, None]:
        yield 1
        yield 2

    async def scenario() -> None:
        wrapped = watched(stream())
        assert await wrapped.__anext__() == 1
        await wrapped.aclose()

    asyncio.run(scenario())
    assert abandoned == [True]


def test_lifecycle_namespaces_have_passes_through() -> None:
    finished: list[Any] = []
    configured = iterator()
    configured.on_finish.validates(finished.append)
    configured.on_error.notifies(lambda exc: None)
    configured.on_abandon.notifies(lambda: None)

    configured.on_finish.passes_through()
    configured.on_error.passes_through()
    configured.on_abandon.passes_through()

    source = numbers()
    assert configured(source) is source  # nothing left configured


# ---------------------------------------------------------------------------
# one factory, many iterators
# ---------------------------------------------------------------------------


def test_one_factory_wraps_many_iterators_independently() -> None:
    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    first = doubles(numbers())
    second = doubles(numbers())

    assert next(first) == 2
    assert next(second) == 2
    assert next(first) == 4
    assert list(second) == [4, 6]


# ---------------------------------------------------------------------------
# generator protocol preservation
# ---------------------------------------------------------------------------


def test_wrapped_generator_is_a_generator() -> None:
    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    assert inspect.isgenerator(doubles(numbers()))


def test_send_is_forwarded_to_the_wrapped_generator() -> None:
    def accumulator() -> Generator[int, int, str]:
        total = 0
        while True:
            sent = yield total
            if sent is None:
                return f"total={total}"
            total += sent

    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    wrapped = doubles(accumulator())
    assert next(wrapped) == 0
    assert wrapped.send(3) == 6  # inner total 3, doubled on the way out
    assert wrapped.send(4) == 14  # inner total 7

    with pytest.raises(StopIteration) as exc:
        wrapped.send(None)
    assert exc.value.value == "total=7"  # return value preserved


def test_throw_is_forwarded_to_the_wrapped_generator() -> None:
    def resilient() -> Generator[str, Any, None]:
        try:
            yield "first"
        except ValueError:
            yield "caught"

    shouting = iterator()
    shouting.on_item.transforms_item(str.upper)

    wrapped = shouting(resilient())
    assert next(wrapped) == "FIRST"
    assert wrapped.throw(ValueError()) == "CAUGHT"


def test_close_closes_the_wrapped_generator() -> None:
    closed: list[bool] = []

    def source() -> Generator[int, None, None]:
        try:
            yield 1
        finally:
            closed.append(True)

    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    wrapped = doubles(source())
    assert next(wrapped) == 2
    wrapped.close()
    assert closed == [True]


def test_stage_error_closes_the_wrapped_generator() -> None:
    closed: list[bool] = []

    def source() -> Generator[int, None, None]:
        try:
            yield 1
        finally:
            closed.append(True)

    def broken(item: int) -> None:
        raise ValueError("bad item")

    checked = iterator()
    checked.on_item.validates_item(broken)

    with pytest.raises(ValueError, match="bad item"):
        next(checked(source()))
    assert closed == [True]


def test_exception_from_the_wrapped_generator_propagates() -> None:
    def failing() -> Generator[int, None, None]:
        yield 1
        raise RuntimeError("boom")

    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    wrapped = doubles(failing())
    assert next(wrapped) == 2
    with pytest.raises(RuntimeError, match="boom"):
        next(wrapped)


# ---------------------------------------------------------------------------
# plain iterators
# ---------------------------------------------------------------------------


def test_wrapping_a_plain_iterator() -> None:
    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    wrapped = doubles(iter([1, 2, 3]))
    assert not inspect.isgenerator(wrapped)
    assert iter(wrapped) is wrapped
    assert list(wrapped) == [2, 4, 6]


def test_wrapping_a_container_is_refused() -> None:
    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    with pytest.raises(TypeError, match="not an iterator"):
        doubles([1, 2, 3])

    with pytest.raises(TypeError, match="not an iterator"):
        doubles("text")

    # refused consistently even with no behaviour configured
    with pytest.raises(TypeError):
        iterator()([1, 2, 3])


def test_wrapping_a_non_iterable_is_refused() -> None:
    with pytest.raises(TypeError, match="not an iterator"):
        iterator()(42)


# ---------------------------------------------------------------------------
# async
# ---------------------------------------------------------------------------


def test_wrapping_an_async_generator() -> None:
    async def stream() -> AsyncGenerator[int, None]:
        yield 1
        yield 2

    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    async def drain() -> list[int]:
        return [item async for item in doubles(stream())]

    assert asyncio.run(drain()) == [2, 4]


def test_aclose_closes_the_wrapped_async_generator() -> None:
    closed: list[bool] = []

    async def stream() -> AsyncGenerator[int, None]:
        try:
            yield 1
        finally:
            closed.append(True)

    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    async def scenario() -> None:
        wrapped = doubles(stream())
        assert await wrapped.__anext__() == 2
        await wrapped.aclose()

    asyncio.run(scenario())
    assert closed == [True]


def test_wrapping_a_plain_async_iterator() -> None:
    class Counter:
        def __init__(self, limit: int) -> None:
            self.count = 0
            self.limit = limit

        def __aiter__(self) -> "Counter":
            return self

        async def __anext__(self) -> int:
            if self.count >= self.limit:
                raise StopAsyncIteration
            self.count += 1
            return self.count

    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    async def drain() -> list[int]:
        return [item async for item in doubles(Counter(3))]

    assert asyncio.run(drain()) == [2, 4, 6]


# ---------------------------------------------------------------------------
# composing with a binding
# ---------------------------------------------------------------------------


class Repo:
    def rows(self, count: int) -> Generator[int, None, None]:
        yield from range(count)


def test_factory_composes_with_transforms_result() -> None:
    doubles = iterator()
    doubles.on_item.transforms_item(lambda item: 2 * item)

    rows = binding(Repo, "rows").on_call.transforms_result(doubles)

    with rows:
        assert list(Repo().rows(3)) == [0, 2, 4]

    assert list(Repo().rows(3)) == [0, 1, 2]  # restored


def test_factory_composes_with_decorates_for_arguments() -> None:
    class Sink:
        def consume(self, stream: Any) -> list[int]:
            return list(stream)

    seen: list[int] = []
    watched = iterator()
    watched.on_item.validates_item(seen.append)

    def per_item(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        return wrapped(watched(args[0]), *args[1:], **kwargs)

    consume = binding(Sink, "consume").on_call.decorates(per_item)

    with consume:
        assert Sink().consume(iter([1, 2, 3])) == [1, 2, 3]

    assert seen == [1, 2, 3]


# ---------------------------------------------------------------------------
# the chain continuation rule: verbs hand the namespace back
# ---------------------------------------------------------------------------


def test_item_verbs_chain_on_one_namespace() -> None:
    staged = (
        iterator()
        .on_item.transforms_item(lambda item: 2 * item)
        .transforms_item(lambda item: item + 1)
    )

    assert list(staged(numbers())) == [3, 5, 7]


def test_chain_and_separate_statements_configure_identically() -> None:
    chained = (
        iterator()
        .on_item.transforms_item(lambda item: 2 * item)
        .transforms_item(lambda item: item + 1)
    )

    stated = iterator()
    stated.on_item.transforms_item(lambda item: 2 * item)
    stated.on_item.transforms_item(lambda item: item + 1)

    assert list(chained(numbers())) == list(stated(numbers()))


def test_the_namespace_stands_in_for_the_factory() -> None:
    finished: list[Any] = []

    watched = (
        iterator()
        .on_item.transforms_item(lambda item: -item)
        .on_finish.validates(finished.append)
    )

    assert list(watched(numbers())) == [-1, -2, -3]
    assert finished == [None]


def test_the_iterator_namespace_repr_shows_the_factory() -> None:
    watched = iterator().on_item.transforms_item(lambda item: item)

    assert repr(watched) == (
        "<IteratorItemBehaviour of <IteratorProxy 1 behaviour(s)>>"
    )
