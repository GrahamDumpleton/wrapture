"""Iterator proxies: behaviour applied to the items and lifecycle of an
iterator.

A binding's behaviour runs when the target is called, but a callable that
returns a generator or iterator produces its values later, one item at a
time, as the caller iterates. iterator() creates a factory holding
behaviour; calling the factory with an iterator returns a wrapped
iterator that applies the behaviour as the iteration runs. Nothing is
wrapped automatically: the factory is applied from a binding's
decorates(), transforms_result() or transforms_args() stage, or called
directly.
"""

from __future__ import annotations

import inspect
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Callable,
    Generator,
    Iterable,
    Iterator,
)
from typing import Any, NamedTuple, Self

ItemFunction = Callable[[Any], Any]
FinishFunction = Callable[[Any], Any]
ErrorFunction = Callable[[BaseException], Any]
AbandonFunction = Callable[[], Any]


class _Hooks(NamedTuple):
    """The behaviour snapshot a wrapped iterator runs with."""

    stages: tuple[ItemFunction, ...]
    finish: tuple[FinishFunction, ...]
    error: tuple[ErrorFunction, ...]
    abandon: tuple[AbandonFunction, ...]

    def item(self, item: Any) -> Any:
        for stage in self.stages:
            item = stage(item)
        return item

    def finished(self, value: Any) -> None:
        for check in self.finish:
            check(value)

    def failed(self, exc: BaseException) -> None:
        for fn in self.error:
            fn(exc)

    def abandoned(self) -> None:
        for fn in self.abandon:
            fn()


def _relay(
    generator: Generator[Any, Any, Any], hooks: _Hooks
) -> Generator[Any, Any, Any]:
    """A generator around a generator, applying hooks as it runs.

    Preserves the full generator protocol: values from send() are
    forwarded in, throw() is forwarded so the wrapped generator can
    handle the exception, close() closes the wrapped generator, and the
    wrapped generator's return value is returned. If an item stage
    raises, the wrapped generator is closed before the exception
    propagates.
    """

    operation: tuple[str, Any] = ("send", None)

    while True:
        # Drive the wrapped generator with whatever the consumer last did
        # to this one: a plain next()/send() or a throw().

        try:
            if operation[0] == "send":
                item = generator.send(operation[1])
            else:
                item = generator.throw(operation[1])
        except StopIteration as stop:
            hooks.finished(stop.value)
            return stop.value
        except BaseException as exc:
            hooks.failed(exc)
            raise

        try:
            item = hooks.item(item)
        except BaseException as exc:
            generator.close()
            hooks.failed(exc)
            raise

        try:
            operation = ("send", (yield item))
        except GeneratorExit:
            generator.close()
            hooks.abandoned()
            raise
        except BaseException as exc:
            operation = ("throw", exc)


async def _relay_async(
    generator: AsyncGenerator[Any, Any], hooks: _Hooks
) -> AsyncGenerator[Any, Any]:
    """The async twin of _relay, for async generators.

    Forwards asend() and athrow(), and aclose() closes the wrapped
    generator. Async generators have no return value, so finish hooks
    receive None.
    """

    operation: tuple[str, Any] = ("send", None)

    while True:
        try:
            if operation[0] == "send":
                item = await generator.asend(operation[1])
            else:
                item = await generator.athrow(operation[1])
        except StopAsyncIteration:
            hooks.finished(None)
            return
        except BaseException as exc:
            hooks.failed(exc)
            raise

        try:
            item = hooks.item(item)
        except BaseException as exc:
            await generator.aclose()
            hooks.failed(exc)
            raise

        try:
            operation = ("send", (yield item))
        except GeneratorExit:
            await generator.aclose()
            hooks.abandoned()
            raise
        except BaseException as exc:
            operation = ("throw", exc)


class _ItemIterator:
    """A plain iterator around a plain iterator, applying hooks.

    Used for iterators that are not generators, which have no send(),
    throw() or close() to forward. With no close() there is also no
    abandonment to observe: abandon hooks never fire for these.
    """

    __slots__ = ("_finished", "_hooks", "_iterator")

    def __init__(self, iterator: Iterator[Any], hooks: _Hooks):
        self._iterator = iterator
        self._hooks = hooks
        self._finished = False

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        try:
            item = next(self._iterator)
        except StopIteration as stop:
            # Finish hooks run once, on the first exhaustion; iterating a
            # spent iterator again raises without re-running them.
            if not self._finished:
                self._finished = True
                self._hooks.finished(stop.value)
            raise
        except BaseException as exc:
            self._hooks.failed(exc)
            raise

        try:
            return self._hooks.item(item)
        except BaseException as exc:
            self._hooks.failed(exc)
            raise


class _AsyncItemIterator:
    """The async twin of _ItemIterator, for plain async iterators."""

    __slots__ = ("_finished", "_hooks", "_iterator")

    def __init__(self, iterator: AsyncIterator[Any], hooks: _Hooks):
        self._iterator = iterator
        self._hooks = hooks
        self._finished = False

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        try:
            item = await self._iterator.__anext__()
        except StopAsyncIteration:
            if not self._finished:
                self._finished = True
                self._hooks.finished(None)
            raise
        except BaseException as exc:
            self._hooks.failed(exc)
            raise

        try:
            return self._hooks.item(item)
        except BaseException as exc:
            self._hooks.failed(exc)
            raise


class _IteratorBehaviour:
    """Base for the iterator factory's behaviour namespaces."""

    __slots__ = ("_factory",)

    def __init__(self, factory: IteratorProxy) -> None:
        self._factory = factory


class IteratorItemBehaviour(_IteratorBehaviour):
    """`iterator().on_item`: behaviour applied to each item.

    Mirrors the composing half of a binding's on_call: stages accumulate
    in the order added and each item passes through all of them.
    """

    __slots__ = ()

    def transforms_item(self, fn: ItemFunction) -> IteratorProxy:
        """fn(item) -> item, rewriting each item as it passes through."""

        return self._factory._add(self._factory._item_stages, fn)

    def validates_item(self, check: Callable[[Any], Any]) -> IteratorProxy:
        """check(item); each item passes through unchanged.

        The check fails the iteration only by raising; its return value
        is ignored.
        """

        def stage(item: Any) -> Any:
            check(item)
            return item

        return self._factory._add(self._factory._item_stages, stage)

    def passes_through(self) -> IteratorProxy:
        """Drop all configured item behaviour."""

        return self._factory._clear(self._factory._item_stages)


class IteratorFinishBehaviour(_IteratorBehaviour):
    """`iterator().on_finish`: behaviour for normal exhaustion.

    Checks receive the wrapped generator's return value, or None for
    iterator kinds that have no return value.
    """

    __slots__ = ()

    def validates(self, check: FinishFunction) -> IteratorProxy:
        """check(value); completion stands unless check raises."""

        return self._factory._add(self._factory._finish_checks, check)

    def passes_through(self) -> IteratorProxy:
        """Drop all configured finish behaviour."""

        return self._factory._clear(self._factory._finish_checks)


class IteratorErrorBehaviour(_IteratorBehaviour):
    """`iterator().on_error`: behaviour for a failed iteration.

    Hooks receive the exception about to reach the consumer, whether it
    came from the wrapped iterator's body, from an unhandled throw(), or
    from an item stage. The exception propagates afterwards; a hook that
    itself raises replaces it.
    """

    __slots__ = ()

    def notifies(self, fn: ErrorFunction) -> IteratorProxy:
        """fn(exc), called before the exception propagates."""

        return self._factory._add(self._factory._error_hooks, fn)

    def passes_through(self) -> IteratorProxy:
        """Drop all configured error behaviour."""

        return self._factory._clear(self._factory._error_hooks)


class IteratorAbandonBehaviour(_IteratorBehaviour):
    """`iterator().on_abandon`: behaviour for an abandoned iteration.

    Hooks fire when a started, unexhausted wrapped generator is closed,
    whether explicitly via close() or by garbage collection. A wrapper
    closed before its first item is silent, and plain iterators have no
    close protocol so never report abandonment.
    """

    __slots__ = ()

    def notifies(self, fn: AbandonFunction) -> IteratorProxy:
        """fn(), called after the wrapped generator has been closed."""

        return self._factory._add(self._factory._abandon_hooks, fn)

    def passes_through(self) -> IteratorProxy:
        """Drop all configured abandon behaviour."""

        return self._factory._clear(self._factory._abandon_hooks)


class IteratorProxy:
    """A factory for iterators that apply configured behaviour.

    Created by iterator(). Unlike a Binding there is no target: one
    factory can be applied to any number of iterators by calling it, and
    each call returns a new wrapped iterator around the one given.

    The behaviour applied by a wrapped iterator is the behaviour
    configured at the moment the factory was called on it. Reconfiguring
    the factory affects only iterators wrapped afterwards.
    """

    def __init__(self) -> None:
        self._item_stages: list[ItemFunction] = []
        self._finish_checks: list[FinishFunction] = []
        self._error_hooks: list[ErrorFunction] = []
        self._abandon_hooks: list[AbandonFunction] = []

    @property
    def on_item(self) -> IteratorItemBehaviour:
        """The behaviour namespace for items."""

        return IteratorItemBehaviour(self)

    @property
    def on_finish(self) -> IteratorFinishBehaviour:
        """The behaviour namespace for normal exhaustion."""

        return IteratorFinishBehaviour(self)

    @property
    def on_error(self) -> IteratorErrorBehaviour:
        """The behaviour namespace for a failed iteration."""

        return IteratorErrorBehaviour(self)

    @property
    def on_abandon(self) -> IteratorAbandonBehaviour:
        """The behaviour namespace for an abandoned iteration."""

        return IteratorAbandonBehaviour(self)

    def _add(self, hooks: list[Any], fn: Any) -> Self:
        hooks.append(fn)
        return self

    def _clear(self, hooks: list[Any]) -> Self:
        hooks.clear()
        return self

    def _snapshot(self) -> _Hooks:
        return _Hooks(
            tuple(self._item_stages),
            tuple(self._finish_checks),
            tuple(self._error_hooks),
            tuple(self._abandon_hooks),
        )

    def __repr__(self) -> str:
        count = (
            len(self._item_stages)
            + len(self._finish_checks)
            + len(self._error_hooks)
            + len(self._abandon_hooks)
        )
        return f"<IteratorProxy {count} behaviour(s)>"

    def __call__(self, iterable: Any) -> Any:
        """Wrap an iterator so configured behaviour applies as it runs.

        Accepts sync and async generators, which keep their full protocol
        through the wrapper, and plain sync and async iterators. With no
        behaviour configured the iterator is returned unwrapped.

        An iterable that is not an iterator, such as a list, is refused:
        wrapping it would silently replace it with an iterator of a
        different type. Call iter() on it first if that is intended.
        """

        # Classify before the no-behaviour shortcut, so an unsupported
        # value is refused consistently however the factory is configured.

        wrap: Callable[[Any, _Hooks], Any]

        if inspect.isgenerator(iterable):
            wrap = _relay
        elif inspect.isasyncgen(iterable):
            wrap = _relay_async
        elif isinstance(iterable, Iterator):
            wrap = _ItemIterator
        elif isinstance(iterable, AsyncIterator):
            wrap = _AsyncItemIterator
        elif isinstance(iterable, Iterable | AsyncIterable):
            raise TypeError(
                f"{iterable!r} is iterable but is not an iterator; wrapping"
                f" it would silently replace it with an iterator of a"
                f" different type. Call iter() on it first if that is"
                f" intended."
            )
        else:
            raise TypeError(f"{iterable!r} is not an iterator")

        hooks = self._snapshot()

        if not any(hooks):
            return iterable

        return wrap(iterable, hooks)


def iterator() -> IteratorProxy:
    """Create an iterator proxy factory.

    The factory holds behaviour configured through its namespaces, and is
    applied by calling it with an iterator; each call returns a new
    wrapped iterator applying the behaviour configured at that moment:

        doubles = wrapture.iterator()
        doubles.on_item.transforms_item(lambda item: 2 * item)

        rows = wrapture.binding(Repo, "rows")
        rows.on_call.transforms_result(doubles)
    """

    return IteratorProxy()
