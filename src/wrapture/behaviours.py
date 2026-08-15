"""Behaviour namespaces for bindings.

Behaviour is what a binding does to the operation it intercepts:
substitute a result, raise an exception, transform arguments or results,
validate them in flight, or wrap the whole operation with a decorator.

Behaviour is scoped by operation. A callable binding exposes on_call; an
attribute binding exposes on_get, on_set and on_delete. Configured
behaviour forms a pipeline per operation: composing stages (transforms_*
and validates_*) wrap around what follows and accumulate in the order
added, while a terminal (returns / raises / decorates / rejects) decides
what happens at the centre and replaces any previous terminal.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

if TYPE_CHECKING:
    from .bindings import Binding

# The signature wrapt uses for wrappers and decorators:
# fn(wrapped, instance, args, kwargs).

WrappedFunction = Callable[..., Any]
WrapperFunction = Callable[[WrappedFunction, Any, tuple[Any, ...], dict[str, Any]], Any]

# A composing stage has the same shape as a wrapper, except that its first
# argument is a forward() callable that invokes the rest of the pipeline.

StageFunction = WrapperFunction


def _then(outcome: Any, fn: Callable[[Any], Any]) -> Any:
    """Apply `fn` to `outcome`, awaiting first if it is awaitable.

    Result-side pipeline stages use this so that on an async target the
    stage applies to the awaited value rather than to the coroutine.
    """

    if inspect.isawaitable(outcome):

        async def resolve() -> Any:
            return fn(await outcome)

        return resolve()
    return fn(outcome)


def _compose(
    pipeline: Sequence[StageFunction], terminal: WrapperFunction | None
) -> WrapperFunction:
    """Build one callable(wrapped, instance, args, kwargs) from the stages.

    Each composing stage is called as stage(forward, instance, args, kwargs)
    where forward(*args, **kwargs) invokes the rest of the chain, so a stage
    can alter what goes in, what comes back, or both. The first stage added
    is outermost. A terminal of None means "perform the real operation".
    """

    def innermost(
        wrapped: WrappedFunction,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if terminal is None:
            return wrapped(*args, **kwargs)
        return terminal(wrapped, instance, args, kwargs)

    chain: WrapperFunction = innermost
    for stage in reversed(pipeline):
        chain = _stage_wrapper(stage, chain)

    return chain


def _stage_wrapper(stage: StageFunction, nxt: WrapperFunction) -> WrapperFunction:
    def call(
        wrapped: WrappedFunction,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        def forward(*a: Any, **k: Any) -> Any:
            return nxt(wrapped, instance, a, k)

        return stage(forward, instance, args, kwargs)

    return call


class _Behaviour:
    """Base for the per-operation behaviour namespaces."""

    __slots__ = ("_binding",)

    _operation: ClassVar[str]

    def __init__(self, bnd: Binding) -> None:
        self._binding = bnd

    def _terminal(self, fn: WrapperFunction) -> Binding:
        self._binding._set_terminal(self._operation, fn)
        return self._binding

    def _stage(self, fn: StageFunction) -> Binding:
        self._binding._add_stage(self._operation, fn)
        return self._binding

    def raises(self, exc: BaseException | type[BaseException]) -> Binding:
        """Raise `exc` instead of performing the operation. Terminal."""

        def boom(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> NoReturn:
            raise exc

        return self._terminal(boom)

    def passes_through(self) -> Binding:
        """Drop all configured behaviour for this operation: both the
        terminal and every composing stage."""

        self._binding._clear_behaviour(self._operation)
        return self._binding


class CallBehaviour(_Behaviour):
    """`binding.on_call`: behaviour for calls to a wrapped callable."""

    __slots__ = ()

    _operation = "call"

    # -- terminal ---------------------------------------------------------

    def returns(self, value: Any) -> Binding:
        """Return `value`; the real callable is never invoked. Terminal."""

        return self._terminal(lambda nxt, i, a, k: value)

    def decorates(self, fn: WrapperFunction) -> Binding:
        """Wrap the real callable: fn(wrapped, instance, args, kwargs).

        This is wrapt's own decorator signature, so a function can move
        between a production decorator and an interceptor unedited.
        Terminal: it decides whether and how the real callable is invoked.
        """

        return self._terminal(fn)

    # -- composing --------------------------------------------------------

    def transforms_args(
        self,
        fn: Callable[
            [tuple[Any, ...], dict[str, Any]],
            tuple[tuple[Any, ...], dict[str, Any]],
        ],
    ) -> Binding:
        """fn(args, kwargs) -> (args, kwargs), rewriting the inbound call."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            new_args, new_kwargs = fn(args, kwargs)
            return nxt(*new_args, **new_kwargs)

        return self._stage(stage)

    def transforms_result(self, fn: Callable[[Any], Any]) -> Binding:
        """fn(result) -> result, rewriting what came back.

        Await-aware: when the target is async, the transform is applied to
        the awaited value rather than to the coroutine.
        """

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return _then(nxt(*args, **kwargs), fn)

        return self._stage(stage)

    def validates_args(self, check: Callable[..., Any]) -> Binding:
        """check(*args, **kwargs); the call passes through unchanged."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            check(*args, **kwargs)
            return nxt(*args, **kwargs)

        return self._stage(stage)

    def validates_result(self, check: Callable[[Any], Any]) -> Binding:
        """check(result); the result passes through unchanged.

        Await-aware: when the target is async, the check sees the awaited
        value rather than the coroutine.
        """

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            def verify(result: Any) -> Any:
                check(result)
                return result

            return _then(nxt(*args, **kwargs), verify)

        return self._stage(stage)


class GetBehaviour(_Behaviour):
    """`binding.on_get`: behaviour for attribute reads.

    The real read is a zero-argument operation producing the value.
    """

    __slots__ = ()

    _operation = "get"

    def returns(self, value: Any) -> Binding:
        """Reading gives `value`; the real read never happens. Terminal."""

        return self._terminal(lambda nxt, i, a, k: value)

    def decorates(self, fn: Callable[[Callable[[], Any], Any], Any]) -> Binding:
        """Wrap the real read: fn(read, instance) -> value, where read()
        performs the read. Terminal."""

        def terminal(
            wrapped: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return fn(wrapped, instance)

        return self._terminal(terminal)

    def transforms(self, fn: Callable[[Any], Any]) -> Binding:
        """fn(value) -> value, rewriting the value read."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return fn(nxt())

        return self._stage(stage)

    def validates(self, check: Callable[[Any], Any]) -> Binding:
        """check(value); the read passes through unchanged."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            value = nxt()
            check(value)
            return value

        return self._stage(stage)


class SetBehaviour(_Behaviour):
    """`binding.on_set`: behaviour for attribute writes.

    The real write takes the value and produces nothing, so there is no
    returns().
    """

    __slots__ = ()

    _operation = "set"

    def rejects(self) -> Binding:
        """Raise AttributeError instead of writing. Terminal."""

        binding = self._binding

        def terminal(
            wrapped: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> NoReturn:
            raise AttributeError(f"can't set attribute {binding._name!r}")

        return self._terminal(terminal)

    def decorates(self, fn: Callable[[Callable[[Any], Any], Any, Any], Any]) -> Binding:
        """Wrap the real write: fn(write, instance, value), where
        write(value) performs the write. Terminal."""

        def terminal(
            wrapped: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return fn(wrapped, instance, args[0])

        return self._terminal(terminal)

    def transforms(self, fn: Callable[[Any], Any]) -> Binding:
        """fn(value) -> value, rewriting the value actually written."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return nxt(fn(args[0]))

        return self._stage(stage)

    def validates(self, check: Callable[[Any], Any]) -> Binding:
        """check(value); the write passes through unchanged."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            check(args[0])
            return nxt(args[0])

        return self._stage(stage)


class DeleteBehaviour(_Behaviour):
    """`binding.on_delete`: behaviour for attribute deletes.

    The real delete takes nothing and produces nothing.
    """

    __slots__ = ()

    _operation = "delete"

    def rejects(self) -> Binding:
        """Raise AttributeError instead of deleting. Terminal."""

        binding = self._binding

        def terminal(
            wrapped: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> NoReturn:
            raise AttributeError(f"can't delete attribute {binding._name!r}")

        return self._terminal(terminal)

    def decorates(self, fn: Callable[[Callable[[], Any], Any], Any]) -> Binding:
        """Wrap the real delete: fn(erase, instance), where erase()
        performs the delete. Terminal."""

        def terminal(
            wrapped: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return fn(wrapped, instance)

        return self._terminal(terminal)

    def validates(self, check: Callable[[Any], Any]) -> Binding:
        """check(instance); the delete passes through unchanged."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            check(instance)
            return nxt()

        return self._stage(stage)
