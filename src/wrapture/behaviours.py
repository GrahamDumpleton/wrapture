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

# The namespaces are generic in R, what a behaviour verb hands back: the
# Binding from a base namespace, so configuration chains into apply() or
# the context manager, and the phase namespace itself from a phase, so
# one phase configures in a chain.

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


class Phase:
    """One phase of a binding's behaviour for a single operation.

    A phase is a complete behaviour: composing stages around at most one
    terminal, with the composed form cached until either changes. Phases
    form a chain per operation, each holding its successor and the exit
    condition that hands over to it; a binding that never calls then()
    has a single phase and behaves as a plain pipeline.
    """

    __slots__ = (
        "index",
        "stages",
        "terminal",
        "injected",
        "_composed",
        "successor",
        "exit",
        "handled",
    )

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.stages: list[StageFunction] = []
        self.terminal: WrapperFunction | None = None
        self.injected = False
        self._composed: WrapperFunction | None = None

        # Chain bookkeeping: the phase that takes over, the condition
        # under which it does, and how many operations this phase has
        # handled since it became active.

        self.successor: Phase | None = None
        self.exit: tuple[str, Any] | None = None
        self.handled = 0

    @property
    def configured(self) -> bool:
        """Whether any stage or terminal has been set."""

        return bool(self.stages) or self.terminal is not None

    def set_terminal(self, fn: WrapperFunction, *, injected: bool = False) -> None:
        self.terminal = fn
        self.injected = injected
        self._composed = None

    def add_stage(self, fn: StageFunction) -> None:
        self.stages.append(fn)
        self._composed = None

    def clear(self) -> None:
        """Drop stages and terminal, so the phase performs the real operation."""

        self.stages = []
        self.terminal = None
        self.injected = False
        self._composed = None

    def behaviour(self) -> WrapperFunction | None:
        """The composed pipeline, or None when nothing is configured."""

        if not self.configured:
            return None

        if self._composed is None:
            self._composed = _compose(self.stages, self.terminal)

        return self._composed


class _Behaviour[R]:
    """Base for the per-operation behaviour namespaces.

    A namespace configures one phase of one operation. The base
    namespaces (`on_call` and friends) configure phase 0 and hand the
    Binding back from every verb; a phase namespace, obtained from
    then(), configures its own phase and hands itself back.
    """

    __slots__ = ("_binding", "_phase")

    _operation: ClassVar[str]

    def __init__(self, bnd: Binding, phase: Phase | None = None) -> None:
        self._binding = bnd
        self._phase = phase

    def _done(self) -> R:
        raise NotImplementedError

    def _current(self) -> Phase:
        """The phase this namespace configures."""

        if self._phase is not None:
            return self._phase

        return self._binding._head(self._operation)

    def _terminal(self, fn: WrapperFunction, *, injected: bool = False) -> R:
        self._binding._set_terminal(
            self._operation, fn, injected=injected, phase=self._phase
        )
        return self._done()

    def _stage(self, fn: StageFunction) -> R:
        self._binding._add_stage(self._operation, fn, phase=self._phase)
        return self._done()

    def _successor(
        self, after: int | None, until: Callable[[Any], Any] | None
    ) -> Phase:
        """Create or fetch the successor of the phase this namespace
        configures, recording the exit condition on this phase."""

        if after is not None and until is not None:
            raise TypeError("then() takes after= or until=, not both")

        if after is not None and (
            isinstance(after, bool) or not isinstance(after, int) or after < 1
        ):
            raise ValueError(f"after= must be a positive int, got {after!r}")

        exit: tuple[str, Any] | None = None
        if after is not None:
            exit = ("after", after)
        elif until is not None:
            exit = ("until", until)

        return self._binding._then(self._operation, self._current(), exit)

    @property
    def phase(self) -> int:
        """Index of the phase currently deciding this operation."""

        return self._binding._phase_index(self._operation)

    def advance(self) -> R:
        """Move this operation to its next phase, whatever the current
        phase's exit condition. A no-op past the last phase."""

        self._binding._advance(self._operation)
        return self._done()

    def raises(self, exc: BaseException | type[BaseException]) -> R:
        """Raise `exc` instead of performing the operation. Terminal."""

        def boom(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> NoReturn:
            raise exc

        return self._terminal(boom, injected=True)

    def passes_through(self) -> R:
        """Perform the real operation: drop this phase's stages and
        terminal. On a base namespace this drops the whole chain of
        phases for the operation."""

        if self._phase is None:
            self._binding._clear_behaviour(self._operation)
        else:
            self._phase.clear()

        return self._done()


class _CallVerbs[R](_Behaviour[R]):
    """The verbs for calls to a wrapped callable, shared by `on_call`
    and the phases chained from it."""

    __slots__ = ()

    _operation = "call"

    def then(
        self, *, after: int | None = None, until: Callable[[Any], Any] | None = None
    ) -> CallPhase:
        """The phase that takes over from this one, created on first call.

        The argument is this phase's exit condition: after=n hands over
        once this phase has handled n more operations, until=fn once
        fn(event) is true for a completed operation, and neither means
        the phase ends only on advance(). Calling then() again returns
        the same successor; an argument on the repeat call replaces the
        exit condition, a bare repeat leaves it alone.
        """

        return CallPhase(self._binding, self._successor(after, until))

    # -- terminal ---------------------------------------------------------

    def returns(self, value: Any) -> R:
        """Return `value`; the real callable is never invoked. Terminal."""

        return self._terminal(lambda nxt, i, a, k: value, injected=True)

    def decorates(self, fn: WrapperFunction) -> R:
        """Wrap the real callable: fn(wrapped, instance, args, kwargs).

        This is wrapt's own wrapper signature, so the function you would
        apply @wrapt.decorator to moves here unedited; pass that
        function, not the result of decorating it. Terminal: it decides
        whether and how the real callable is invoked.
        """

        return self._terminal(fn)

    # -- composing --------------------------------------------------------

    def transforms_args(
        self,
        fn: Callable[
            [tuple[Any, ...], dict[str, Any]],
            tuple[tuple[Any, ...], dict[str, Any]],
        ],
    ) -> R:
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

    def transforms_result(self, fn: Callable[[Any], Any]) -> R:
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

    def validates_args(self, check: Callable[..., Any]) -> R:
        """check(*args, **kwargs); the call passes through unchanged.

        The check fails the call only by raising; its return value is
        ignored, so returning False fails nothing.
        """

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            check(*args, **kwargs)
            return nxt(*args, **kwargs)

        return self._stage(stage)

    def validates_result(self, check: Callable[[Any], Any]) -> R:
        """check(result); the result passes through unchanged.

        The check fails the call only by raising; its return value is
        ignored. Await-aware: when the target is async, the check sees
        the awaited value rather than the coroutine.
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


class _GetVerbs[R](_Behaviour[R]):
    """The verbs for attribute reads, shared by `on_get` and its phases.

    The real read is a zero-argument operation producing the value.
    """

    __slots__ = ()

    _operation = "get"

    def then(
        self, *, after: int | None = None, until: Callable[[Any], Any] | None = None
    ) -> GetPhase:
        """The phase that takes over from this one, created on first call.

        The argument is this phase's exit condition: after=n hands over
        once this phase has handled n more operations, until=fn once
        fn(event) is true for a completed operation, and neither means
        the phase ends only on advance(). Calling then() again returns
        the same successor; an argument on the repeat call replaces the
        exit condition, a bare repeat leaves it alone.
        """

        return GetPhase(self._binding, self._successor(after, until))

    def returns(self, value: Any) -> R:
        """Reading gives `value`; the real read never happens. Terminal."""

        return self._terminal(lambda nxt, i, a, k: value, injected=True)

    def decorates(self, fn: Callable[[Callable[[], Any], Any], Any]) -> R:
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

    def transforms(self, fn: Callable[[Any], Any]) -> R:
        """fn(value) -> value, rewriting the value read."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return fn(nxt())

        return self._stage(stage)

    def validates(self, check: Callable[[Any], Any]) -> R:
        """check(value); the read passes through unchanged.

        The check fails the read only by raising; its return value is
        ignored.
        """

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


class _SetVerbs[R](_Behaviour[R]):
    """The verbs for attribute writes, shared by `on_set` and its phases.

    The real write takes the value and produces nothing, so there is no
    returns().
    """

    __slots__ = ()

    _operation = "set"

    def then(
        self, *, after: int | None = None, until: Callable[[Any], Any] | None = None
    ) -> SetPhase:
        """The phase that takes over from this one, created on first call.

        The argument is this phase's exit condition: after=n hands over
        once this phase has handled n more operations, until=fn once
        fn(event) is true for a completed operation, and neither means
        the phase ends only on advance(). Calling then() again returns
        the same successor; an argument on the repeat call replaces the
        exit condition, a bare repeat leaves it alone.
        """

        return SetPhase(self._binding, self._successor(after, until))

    def rejects(self) -> R:
        """Raise AttributeError instead of writing. Terminal."""

        binding = self._binding

        def terminal(
            wrapped: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> NoReturn:
            raise AttributeError(f"can't set attribute {binding._name!r}")

        return self._terminal(terminal, injected=True)

    def decorates(self, fn: Callable[[Callable[[Any], Any], Any, Any], Any]) -> R:
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

    def transforms(self, fn: Callable[[Any], Any]) -> R:
        """fn(value) -> value, rewriting the value actually written."""

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return nxt(fn(args[0]))

        return self._stage(stage)

    def validates(self, check: Callable[[Any], Any]) -> R:
        """check(value); the write passes through unchanged.

        The check fails the write only by raising; its return value is
        ignored.
        """

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            check(args[0])
            return nxt(args[0])

        return self._stage(stage)


class _DeleteVerbs[R](_Behaviour[R]):
    """The verbs for attribute deletes, shared by `on_delete` and its
    phases.

    The real delete takes nothing and produces nothing.
    """

    __slots__ = ()

    _operation = "delete"

    def then(
        self, *, after: int | None = None, until: Callable[[Any], Any] | None = None
    ) -> DeletePhase:
        """The phase that takes over from this one, created on first call.

        The argument is this phase's exit condition: after=n hands over
        once this phase has handled n more operations, until=fn once
        fn(event) is true for a completed operation, and neither means
        the phase ends only on advance(). Calling then() again returns
        the same successor; an argument on the repeat call replaces the
        exit condition, a bare repeat leaves it alone.
        """

        return DeletePhase(self._binding, self._successor(after, until))

    def rejects(self) -> R:
        """Raise AttributeError instead of deleting. Terminal."""

        binding = self._binding

        def terminal(
            wrapped: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> NoReturn:
            raise AttributeError(f"can't delete attribute {binding._name!r}")

        return self._terminal(terminal, injected=True)

    def decorates(self, fn: Callable[[Callable[[], Any], Any], Any]) -> R:
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

    def validates(self, check: Callable[[Any], Any]) -> R:
        """check(instance); the delete passes through unchanged.

        The check fails the delete only by raising; its return value is
        ignored.
        """

        def stage(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            check(instance)
            return nxt()

        return self._stage(stage)


# The concrete namespaces. A base namespace configures phase 0 and
# returns the Binding from its verbs; a phase namespace configures the
# phase then() created and returns itself.


class CallBehaviour(_CallVerbs["Binding"]):
    """`binding.on_call`: behaviour for calls to a wrapped callable."""

    __slots__ = ()

    def _done(self) -> Binding:
        return self._binding


class CallPhase(_CallVerbs["CallPhase"]):
    """One phase of call behaviour, from `on_call.then()`."""

    __slots__ = ()

    def _done(self) -> CallPhase:
        return self


class GetBehaviour(_GetVerbs["Binding"]):
    """`binding.on_get`: behaviour for attribute reads."""

    __slots__ = ()

    def _done(self) -> Binding:
        return self._binding


class GetPhase(_GetVerbs["GetPhase"]):
    """One phase of read behaviour, from `on_get.then()`."""

    __slots__ = ()

    def _done(self) -> GetPhase:
        return self


class SetBehaviour(_SetVerbs["Binding"]):
    """`binding.on_set`: behaviour for attribute writes."""

    __slots__ = ()

    def _done(self) -> Binding:
        return self._binding


class SetPhase(_SetVerbs["SetPhase"]):
    """One phase of write behaviour, from `on_set.then()`."""

    __slots__ = ()

    def _done(self) -> SetPhase:
        return self


class DeleteBehaviour(_DeleteVerbs["Binding"]):
    """`binding.on_delete`: behaviour for attribute deletes."""

    __slots__ = ()

    def _done(self) -> Binding:
        return self._binding


class DeletePhase(_DeleteVerbs["DeletePhase"]):
    """One phase of delete behaviour, from `on_delete.then()`."""

    __slots__ = ()

    def _done(self) -> DeletePhase:
        return self
