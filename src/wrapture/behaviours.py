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
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

if TYPE_CHECKING:
    from .bindings import Binding

# The namespaces are generic in R, what a behaviour verb hands back: the
# namespace the chain first encountered. A base namespace hands itself
# back, so verbs chain on one channel without naming it again, and the
# namespace stands in for its binding (apply(), the context manager,
# timeline()); a phase namespace, from then(), likewise hands itself
# back so one phase configures in a chain.

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


def _deliver_async(terminal: WrapperFunction, kind: str) -> WrapperFunction:
    """Wrap an injecting terminal so its outcome arrives as the target's
    calling protocol delivers it: a value or exception on await for a
    coroutine function, items or the exception on iteration for an
    async generator function. An outcome that already fits (an
    awaitable, an async iterable) passes through. Exhaustion of a
    returns_from() sequence still surfaces at call time, where the
    dispatch loop hands over to the next phase."""

    def delivered(
        wrapped: WrappedFunction,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        try:
            outcome = terminal(wrapped, instance, args, kwargs)
        except _Exhausted:
            raise
        except BaseException as exc:
            if kind == "asyncgen":
                return _named_after(_fail_iteration(exc), wrapped)
            return _named_after(_raise_later(exc), wrapped)

        if kind == "asyncgen":
            if hasattr(outcome, "__aiter__"):
                return outcome
            return _named_after(_iterate_later(outcome), wrapped)

        if inspect.isawaitable(outcome):
            return outcome
        return _named_after(_resolve_later(outcome), wrapped)

    return delivered


def _named_after(delivery: Any, source: Any) -> Any:
    """Name a coroutine or async generator after `source` (a target, or
    the coroutine it wraps), so the interpreter's "coroutine 'X' was
    never awaited" warning and any repr name the call being made rather
    than the wrapture helper that produced the object."""

    for attribute in ("__name__", "__qualname__"):
        name = getattr(source, attribute, None)
        if isinstance(name, str):
            try:
                setattr(delivery, attribute, name)
            except (AttributeError, TypeError):
                pass

    return delivery


async def _resolve_later(value: Any) -> Any:
    return value


async def _raise_later(exc: BaseException) -> NoReturn:
    raise exc


async def _iterate_later(iterable: Iterable[Any]) -> AsyncIterator[Any]:
    for item in iterable:
        yield item


async def _fail_iteration(exc: BaseException) -> AsyncIterator[Any]:
    raise exc
    yield  # makes this an async generator; never reached


class _Exhausted(BaseException):
    """Raised by a returns_from() terminal when its sequence runs out.

    A BaseException so that stages catching Exception around the rest of
    the pipeline do not swallow it; the binding's dispatch turns it into
    a hand-over to the successor phase.
    """


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
        "source",
        "iterator",
        "draw_lock",
    )

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.stages: list[StageFunction] = []
        self.terminal: WrapperFunction | None = None
        self.injected = False
        self._composed: dict[str | None, WrapperFunction] = {}

        # Chain bookkeeping: the phase that takes over, the condition
        # under which it does, and how many operations this phase has
        # handled since it became active.

        self.successor: Phase | None = None
        self.exit: tuple[str, Any] | None = None
        self.handled = 0

        # A returns_from() sequence: the iterable as given, so apply()
        # can restart it with a fresh iter(), the live iterator, and a
        # lock so concurrent operations draw one value each.

        self.source: Iterable[Any] | None = None
        self.iterator: Iterator[Any] | None = None
        self.draw_lock: threading.RLock | None = None

    @property
    def configured(self) -> bool:
        """Whether any stage or terminal has been set."""

        return bool(self.stages) or self.terminal is not None

    @property
    def watches(self) -> bool:
        """Whether this phase ends on an until= predicate, so completed
        operations must be shown to it."""

        return self.exit is not None and self.exit[0] == "until"

    def set_terminal(self, fn: WrapperFunction, *, injected: bool = False) -> None:
        self.terminal = fn
        self.injected = injected
        self._composed.clear()
        self.source = None
        self.iterator = None
        self.draw_lock = None

    def set_sequence(self, iterable: Iterable[Any]) -> None:
        """Make the terminal draw successive values from `iterable`."""

        phase = self

        def draw(
            nxt: WrappedFunction,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            assert phase.iterator is not None and phase.draw_lock is not None

            with phase.draw_lock:
                try:
                    return next(phase.iterator)
                except StopIteration:
                    raise _Exhausted from None

        self.set_terminal(draw, injected=True)
        self.source = iterable
        self.iterator = iter(iterable)
        self.draw_lock = threading.RLock()

    def restart(self) -> None:
        """Reset the handled count and restart any sequence from the
        iterable it was given."""

        self.handled = 0

        if self.source is not None:
            self.iterator = iter(self.source)

    def add_stage(self, fn: StageFunction) -> None:
        self.stages.append(fn)
        self._composed.clear()

    def clear(self) -> None:
        """Drop stages and terminal, so the phase performs the real operation."""

        self.stages = []
        self.terminal = None
        self.injected = False
        self._composed.clear()
        self.source = None
        self.iterator = None
        self.draw_lock = None

    def behaviour(self, async_kind: str | None = None) -> WrapperFunction | None:
        """The composed pipeline, or None when nothing is configured.

        `async_kind` names the calling protocol of the target when it is
        a coroutine function ("coroutine") or an async generator
        function ("asyncgen"): an injecting terminal then delivers its
        outcome the way the real target would, on await or on iteration,
        so the stages around it and the caller see what they would see
        from the real thing. Composed forms are cached per kind.
        """

        if not self.configured:
            return None

        composed = self._composed.get(async_kind)
        if composed is None:
            terminal = self.terminal
            if async_kind is not None and self.injected and terminal is not None:
                terminal = _deliver_async(terminal, async_kind)
            composed = _compose(self.stages, terminal)
            self._composed[async_kind] = composed

        return composed


class _Behaviour[R]:
    """Base for the per-operation behaviour namespaces.

    A namespace configures one phase of one operation, and every verb
    hands the namespace back: the base namespaces (`on_call` and
    friends) configure phase 0, a phase namespace, obtained from
    then(), configures its own phase. Verbs therefore chain without
    naming the channel again, and a base namespace stands in for its
    binding wherever one is expected.
    """

    __slots__ = ("_binding", "_phase")

    _operation: ClassVar[str]

    def __init__(self, bnd: Binding, phase: Phase | None = None) -> None:
        self._binding = bnd
        self._phase = phase

    def _done(self) -> R:
        raise NotImplementedError

    def __repr__(self) -> str:
        index = 0 if self._phase is None else self._phase.index
        return f"<{type(self).__name__} {index} of {self._binding._display!r}>"

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
        """Perform the real operation in this phase: drop the phase's
        stages and terminal. Other phases are untouched; to drop the
        whole chain, use reset() on the base namespace."""

        self._current().clear()
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

    def returns_from(self, iterable: Iterable[Any]) -> R:
        """Return the next value of `iterable` on each call; the real
        callable is never invoked. Terminal.

        The iterable is consumed lazily, one value per call, so a
        generator or itertools.cycle() works, and iter() is called on it
        afresh at each apply(). When it runs out the phase ends and the
        call that found it empty is handled by the successor phase; with
        no successor that call raises SequenceExhaustedError.
        """

        self._current().set_sequence(iterable)
        return self._done()

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

    def returns_from(self, iterable: Iterable[Any]) -> R:
        """Reading gives the next value of `iterable` on each read; the
        real read never happens. Terminal.

        Consumed lazily, one value per read, and restarted with iter()
        at each apply(). When it runs out the phase ends and the read
        that found it empty is handled by the successor phase; with no
        successor that read raises SequenceExhaustedError. Reads are
        easy to trigger by accident (repr, hasattr, a debugger), so pair
        a sequence with a successor or use itertools.cycle().
        """

        self._current().set_sequence(iterable)
        return self._done()

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


class _BaseNamespace:
    """What only a base namespace offers: dropping the whole chain, and
    standing in for the binding.

    Verbs on a base namespace hand the namespace back, so an expression
    like `binding(X, "y").on_call.transforms_args(f).returns(None)`
    chains without naming the channel again and lands somewhere that
    must behave as the binding: a with target, a timeline() argument,
    the object whose `.events` a test reads. The lifecycle methods are
    defined explicitly (the with statement looks dunder methods up on
    the type, and the appliable protocol names apply and remove);
    everything else falls through to the binding.
    """

    __slots__ = ()

    _binding: Binding
    _operation: ClassVar[str]

    def reset(self) -> Binding:
        """Drop all behaviour for this operation, every phase included,
        leaving a bare phase 0 that performs the real operation."""

        self._binding._clear_behaviour(self._operation)
        return self._binding

    def apply(self, *, suspended: bool = False) -> Binding:
        """Apply the binding this namespace configures."""

        return self._binding.apply(suspended=suspended)

    def remove(self, *, missing_ok: bool = True) -> Binding:
        """Remove the binding this namespace configures."""

        return self._binding.remove(missing_ok=missing_ok)

    def __enter__(self) -> Binding:
        return self._binding.__enter__()

    def __exit__(self, *exc: object) -> None:
        self._binding.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        # Underscore names are the namespace's own business: delegating
        # them could recurse before _binding is set, and nothing private
        # to the binding is part of the stand-in surface.

        if name.startswith("_"):
            raise AttributeError(name)

        return getattr(self._binding, name)

    def __repr__(self) -> str:
        # The namespace comes back from every verb, so its repr is what
        # a REPL shows after configuring: name the channel type and show
        # the binding's own state inside.

        return f"<{type(self).__name__} of {self._binding!r}>"


class CallBehaviour(_BaseNamespace, _CallVerbs["CallBehaviour"]):
    """`binding.on_call`: behaviour for calls to a wrapped callable."""

    __slots__ = ()

    def _done(self) -> CallBehaviour:
        return self


class CallPhase(_CallVerbs["CallPhase"]):
    """One phase of call behaviour, from `on_call.then()`."""

    __slots__ = ()

    def _done(self) -> CallPhase:
        return self


class GetBehaviour(_BaseNamespace, _GetVerbs["GetBehaviour"]):
    """`binding.on_get`: behaviour for attribute reads."""

    __slots__ = ()

    def _done(self) -> GetBehaviour:
        return self


class GetPhase(_GetVerbs["GetPhase"]):
    """One phase of read behaviour, from `on_get.then()`."""

    __slots__ = ()

    def _done(self) -> GetPhase:
        return self


class SetBehaviour(_BaseNamespace, _SetVerbs["SetBehaviour"]):
    """`binding.on_set`: behaviour for attribute writes."""

    __slots__ = ()

    def _done(self) -> SetBehaviour:
        return self


class SetPhase(_SetVerbs["SetPhase"]):
    """One phase of write behaviour, from `on_set.then()`."""

    __slots__ = ()

    def _done(self) -> SetPhase:
        return self


class DeleteBehaviour(_BaseNamespace, _DeleteVerbs["DeleteBehaviour"]):
    """`binding.on_delete`: behaviour for attribute deletes."""

    __slots__ = ()

    def _done(self) -> DeleteBehaviour:
        return self


class DeletePhase(_DeleteVerbs["DeletePhase"]):
    """One phase of delete behaviour, from `on_delete.then()`."""

    __slots__ = ()

    def _done(self) -> DeletePhase:
        return self
