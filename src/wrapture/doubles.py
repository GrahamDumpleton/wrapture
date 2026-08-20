"""Stand-ins for callables the test must supply itself.

A binding wraps a callable that already lives somewhere; observed()
wraps one the test has in hand. Both leave the real code running. The
remaining case is the callable the test has to make up: a hook slot
read by name, a receiver handed to a signal, a callback passed into a
registration call, where the test does not care what arrives and only
counts calls or dictates the outcome. stub() builds that stand-in and
returns it already observed: a recording proxy with events, suspend()
and resume(), the honest counters, and timeline participation.

A stub stands in for one callable and that is its whole surface. It
fabricates no attributes and never widens into an object; a test that
needs a many-method collaborator should build one from real parts.

By default a stub accepts any arguments, the explicit statement that
they do not matter here. mimics= opts back into strictness: the stub
borrows a real callable's signature, so calls are checked and recorded
by parameter name, and borrows its kind, so a stub for an async def
is itself awaited.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from typing import Any

from wrapt import MISSING

from .events import _signature
from .observed import ObservedCallable, _describe

KINDS = ("function", "generator", "coroutine", "async_generator")


def _infer_kind(mimics: Callable[..., Any]) -> str:
    # The same resolution order bindings use for async targets, plus
    # the sync generator case, all seeing through wrapt wrappers and
    # bound methods.

    if inspect.iscoroutinefunction(mimics):
        return "coroutine"
    if inspect.isasyncgenfunction(mimics):
        return "async_generator"
    if inspect.isgeneratorfunction(mimics):
        return "generator"

    return "function"


def _stand_in(kind: str, returns: Any, raises: Any) -> Callable[..., Any]:
    # The stand-in is a real function of the stated kind, so calling
    # convention detection through the transparent proxy answers
    # truthfully, and it carries the outcome itself: return or raise
    # for the plain kinds, delivered on await or iteration for the
    # others, exactly as the real callable's protocol would deliver it.

    if kind == "function":

        def stand_in(*args: Any, **kwargs: Any) -> Any:
            if raises is not None:
                raise raises
            return returns

        return stand_in

    if kind == "coroutine":

        async def coroutine_stand_in(*args: Any, **kwargs: Any) -> Any:
            if raises is not None:
                raise raises
            return returns

        return coroutine_stand_in

    if kind == "generator":

        def generator_stand_in(*args: Any, **kwargs: Any) -> Any:
            if raises is not None:
                raise raises
            yield from returns if returns is not None else ()

        return generator_stand_in

    async def async_generator_stand_in(*args: Any, **kwargs: Any) -> Any:
        if raises is not None:
            raise raises
        for item in returns if returns is not None else ():
            yield item

    return async_generator_stand_in


def stub(
    label: str | None = None,
    *,
    returns: Any = MISSING,
    raises: BaseException | type[BaseException] | None = None,
    mimics: Callable[..., Any] | None = None,
    kind: str | None = None,
) -> ObservedCallable:
    """Build a stand-in callable that records, for the test to place.

    With no arguments the stub accepts any call, returns None, and
    records each call as an event inside a recording scope, so
    `events.assert_once()` and the rest apply. `label` names it in
    events and assertions (default "stub"). `returns=` makes every
    call produce that value instead; `raises=` makes every call raise;
    the two are mutually exclusive, and there are no phases: a stand-in
    whose behaviour must change over time should be a real function, or
    a binding on a real location.

    `kind=` sets the calling convention of the stand-in: "function"
    (default), "generator", "coroutine" or "async_generator". The stub
    genuinely is a callable of that kind, so detection through the
    proxy (`inspect.iscoroutinefunction()` and friends) answers
    truthfully, and the outcome arrives as that kind delivers it:
    returned or raised at the call for "function"; on await for
    "coroutine"; yielded per item, or raised on iteration, for the
    generator kinds, whose `returns=` must be an iterable of the items
    to yield.

    `mimics=` borrows both signature and kind from a real callable,
    the deliberate opt back into strictness: calls are checked against
    the borrowed signature, raising TypeError before anything is
    recorded exactly as a strict binding does, events record arguments
    by parameter name so `with_args()` matches them, and the kind is
    inferred (so `kind=` cannot be combined with it). The stub also
    takes the callable's name, so reprs and "coroutine was never
    awaited" warnings name what it stands in for.

    Placed on a class, a stub binds as a method: calls made through
    instances record the instance, and a mimicked signature's `self`
    is accounted for by the binding, as with the real method.
    """

    # returns= and raises= are one outcome each; asking for both is a
    # contradiction rather than a precedence question.

    if returns is not MISSING and raises is not None:
        raise ValueError("stub() takes returns= or raises=, not both")

    if raises is not None:
        acceptable = isinstance(raises, BaseException) or (
            isinstance(raises, type) and issubclass(raises, BaseException)
        )
        if not acceptable:
            raise TypeError(
                f"raises must be an exception instance or class, got {raises!r}"
            )

    # Resolve the kind: inferred from mimics=, stated with kind=, or
    # the plain function default.

    if mimics is not None:
        if not callable(mimics):
            raise TypeError(f"mimics must be a callable, got {mimics!r}")
        if kind is not None:
            raise TypeError("kind is inferred from mimics; give one or the other")

        kind = _infer_kind(mimics)
    elif kind is None:
        kind = "function"
    elif kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")

    # The generator kinds deliver returns= by iterating it, so reject a
    # value that cannot be iterated at construction rather than at the
    # first call.

    returns_value = None if returns is MISSING else returns
    if kind in ("generator", "async_generator") and returns_value is not None:
        if not isinstance(returns_value, Iterable):
            raise TypeError(
                f"returns must be an iterable of items to yield for"
                f" kind={kind!r}, got {returns_value!r}"
            )

    stand_in = _stand_in(kind, returns_value, raises)

    # Identity: a mimicked stub reports and records as what it stands
    # in for; a bare one as its label. The names go onto the stand-in
    # itself so delivered coroutines and generators inherit them, which
    # is what makes a never-awaited warning name the target.

    if mimics is not None:
        path, derived = _describe(mimics)
        effective = label or derived

        stand_in.__name__ = getattr(mimics, "__name__", effective)
        stand_in.__qualname__ = getattr(mimics, "__qualname__", effective)

        # The borrowed signature rides on the stand-in, where every
        # consumer already looks: introspection through the proxy,
        # argument normalization, the strict check, and bound access
        # stripping self, all as for the real callable.

        try:
            stand_in.__signature__ = inspect.signature(mimics)  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            pass
    else:
        effective = label or "stub"
        path = f"stub:{effective}"

        stand_in.__name__ = effective
        stand_in.__qualname__ = effective

    # Strict checking only when there is a signature to check against:
    # a rejected call raises before it is recorded or counted, as with
    # a strict binding, and the check binds what the call site sees, so
    # a stub bound as a method checks without self.

    precheck = None
    if mimics is not None:

        def precheck(
            target: Callable[..., Any],
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> None:
            signature = _signature(target)
            if signature is None:
                return

            try:
                signature.bind(*args, **kwargs)
            except TypeError as exc:
                raise TypeError(f"{effective} (stubbed): {exc}") from None

    return ObservedCallable(
        stand_in,
        path=path,
        label=effective,
        capture_args=None,
        capture_result=None,
        stack=None,
        when=None,
        precheck=precheck,
    )
