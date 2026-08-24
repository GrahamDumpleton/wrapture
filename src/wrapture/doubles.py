"""Stand-ins the test supplies itself: one callable, or one collaborator.

A binding wraps a callable that already lives somewhere; observed()
wraps one the test has in hand. Both leave the real code running. The
remaining cases are the stand-ins a test has to make up. stub() builds
one callable: a hook slot read by name, a receiver handed to a signal,
a callback passed into a registration call, where the test does not
care what arrives and only counts calls or dictates the outcome.
mock() builds one collaborator: an instance-shaped double of a named
class, every method a signature-checked recording stub, for the code
under test to receive through a seam. Both record through the
observed() machinery: events, suspend() and resume(), the honest
counters, and timeline participation.

A mock requires a spec and fabricates nothing beyond it. There is no
spec-less form: the double's surface is exactly the named class's
methods, an absent name raises AttributeError, every method returns
None until configured, and there are no fabricated return-value
chains. A stub stands in for one callable and that is its whole
surface.

By default a stub accepts any arguments, the explicit statement that
they do not matter here. mimics= opts back into strictness, and every
mock() method has it: the borrowed signature checks calls and records
arguments by parameter name, and the borrowed kind makes a stand-in
for an async def itself awaited.
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


class _Outcome:
    """The one configured outcome of a stub, read live on every call so
    returns() and raises() reconfigure a stub already placed."""

    __slots__ = ("returns", "raises")

    def __init__(self) -> None:
        self.returns: Any = None
        self.raises: BaseException | type[BaseException] | None = None


def _check_raises(raises: Any) -> None:
    acceptable = isinstance(raises, BaseException) or (
        isinstance(raises, type) and issubclass(raises, BaseException)
    )
    if not acceptable:
        raise TypeError(
            f"raises must be an exception instance or class, got {raises!r}"
        )


def _check_yields(kind: str, returns: Any) -> None:
    # The generator kinds deliver returns by iterating it, so reject a
    # value that cannot be iterated when it is configured rather than
    # at the first call.

    if kind in ("generator", "async_generator") and returns is not None:
        if not isinstance(returns, Iterable):
            raise TypeError(
                f"returns must be an iterable of items to yield for"
                f" kind={kind!r}, got {returns!r}"
            )


def _stand_in(kind: str, outcome: _Outcome) -> Callable[..., Any]:
    # The stand-in is a real function of the stated kind, so calling
    # convention detection through the transparent proxy answers
    # truthfully, and it delivers the configured outcome itself: return
    # or raise for the plain kinds, on await or iteration for the
    # others, exactly as the real callable's protocol would deliver it.

    if kind == "function":

        def stand_in(*args: Any, **kwargs: Any) -> Any:
            if outcome.raises is not None:
                raise outcome.raises
            return outcome.returns

        return stand_in

    if kind == "coroutine":

        async def coroutine_stand_in(*args: Any, **kwargs: Any) -> Any:
            if outcome.raises is not None:
                raise outcome.raises
            return outcome.returns

        return coroutine_stand_in

    if kind == "generator":

        def generator_stand_in(*args: Any, **kwargs: Any) -> Any:
            if outcome.raises is not None:
                raise outcome.raises
            yield from outcome.returns if outcome.returns is not None else ()

        return generator_stand_in

    async def async_generator_stand_in(*args: Any, **kwargs: Any) -> Any:
        if outcome.raises is not None:
            raise outcome.raises
        for item in outcome.returns if outcome.returns is not None else ():
            yield item

    return async_generator_stand_in


class StubCallable(ObservedCallable):
    """The stand-in stub() returns and every mock() method is.

    An ObservedCallable whose target delivers a configured outcome
    instead of running real code. returns() and raises() set and reset
    that outcome at any time, before or after the stub is placed; each
    replaces the other, and there are no phases.
    """

    _self_kind: str
    _self_outcome: _Outcome

    def returns(self, value: Any) -> StubCallable:
        """Make every call produce this value: returned at the call,
        resolved on await for a coroutine stub, yielded per item for
        the generator kinds (where the value must be iterable).
        Replaces any configured outcome. Returns the stub."""

        _check_yields(self._self_kind, value)

        outcome = self._self_outcome
        outcome.returns = value
        outcome.raises = None
        return self

    def raises(self, exc: BaseException | type[BaseException]) -> StubCallable:
        """Make every call raise this exception: at the call, on await
        for a coroutine stub, on iteration for the generator kinds.
        Replaces any configured outcome. Returns the stub."""

        _check_raises(exc)

        outcome = self._self_outcome
        outcome.raises = exc
        outcome.returns = None
        return self

    @property
    def returns_value(self) -> Any:
        """The configured return value, None until returns() sets one:
        the way to reach a double configured as another method's
        result without holding it in a variable."""

        return self._self_outcome.returns


def stub(
    label: str | None = None,
    *,
    returns: Any = MISSING,
    raises: BaseException | type[BaseException] | None = None,
    mimics: Callable[..., Any] | None = None,
    kind: str | None = None,
) -> StubCallable:
    """Build a stand-in callable that records, for the test to place.

    With no arguments the stub accepts any call, returns None, and
    records each call as an event inside a recording scope, so
    `events.assert_once()` and the rest apply. `label` names it in
    events and assertions (default "stub"). `returns=` makes every
    call produce that value instead; `raises=` makes every call raise;
    the two are mutually exclusive. The same pair exist as methods on
    the returned stub, so an outcome can be set or replaced after
    construction (`hook.returns(42)`, `hook.raises(exc)`), each
    replacing the other; there are no phases beyond that one
    reconfigurable outcome: a stand-in whose behaviour must change over
    time on its own should be a real function, or a binding on a real
    location.

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
        _check_raises(raises)

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

    outcome = _Outcome()
    outcome.returns = None if returns is MISSING else returns
    outcome.raises = raises
    _check_yields(kind, outcome.returns)

    stand_in = _stand_in(kind, outcome)

    # Identity: a mimicked stub reports and records as what it stands
    # in for; a bare one as its label. The names go onto the stand-in
    # itself so delivered coroutines and generators inherit them, which
    # is what makes a never-awaited warning name the target.

    if mimics is not None:
        path = _describe(mimics)
        display = label or path
        fallback = label or path.rpartition(":")[2]

        stand_in.__name__ = getattr(mimics, "__name__", fallback)
        stand_in.__qualname__ = getattr(mimics, "__qualname__", fallback)

        # The borrowed signature rides on the stand-in, where every
        # consumer already looks: introspection through the proxy,
        # argument normalization, the strict check, and bound access
        # stripping self, all as for the real callable.

        try:
            stand_in.__signature__ = inspect.signature(mimics)  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            pass
    else:
        # A bare stub has no call site to derive from, so its path is
        # the standard instance fallback, the type of the callable
        # itself: a colon path that resolves, to the class that says
        # "fabricated double", while the label carries which one.

        label = label or "stub"
        display = label
        path = f"{StubCallable.__module__}:{StubCallable.__qualname__}"

        stand_in.__name__ = label
        stand_in.__qualname__ = label

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
                raise TypeError(f"{display} (stubbed): {exc}") from None

    double = StubCallable(
        stand_in,
        path=path,
        label=label,
        capture_args=None,
        capture_result=None,
        stack=None,
        when=None,
        precheck=precheck,
    )
    double._self_kind = kind
    double._self_outcome = outcome
    return double


def _spec_methods(spec: type) -> dict[str, Any]:
    # The spec's callable surface through the full MRO: plain methods,
    # classmethods and staticmethods, each kept in its descriptor form
    # so the stub can mimic the underlying function. Dunders are never
    # stubs; __enter__/__exit__ get dedicated handling on the double,
    # everything else double-underscored is left alone.

    methods: dict[str, Any] = {}

    for name in dir(spec):
        if name.startswith("__"):
            continue

        attribute = inspect.getattr_static(spec, name)

        if isinstance(attribute, (staticmethod, classmethod)) or callable(attribute):
            methods[name] = attribute

    return methods


def _double_namespace(spec: type) -> dict[str, Any]:
    # The generated class reports the spec as __class__ (so isinstance
    # checks in the code under test hold), refuses names beyond the
    # spec with an error saying so, and reads as a mock in repr.

    def __getattr__(self: Any, name: str) -> Any:
        # Setting name on the error keeps the structured field the
        # interpreter would fill in, while opting out of its appended
        # did-you-mean suggestion, whose wording varies by Python
        # version; the message already names the attribute and spec.

        if hasattr(spec, name):
            error = AttributeError(
                f"{spec.__name__}.{name} is not fabricated: the mock"
                f" holds no value for it; assign it on the double if"
                f" the code under test reads it"
            )
        else:
            error = AttributeError(
                f"{spec.__name__} has no attribute {name!r}; the mock"
                f" fabricates nothing beyond its spec"
            )

        error.name = name
        raise error

    def __repr__(self: Any) -> str:
        return f"<wrapture.mock {spec.__name__}>"

    namespace: dict[str, Any] = {
        "__module__": spec.__module__,
        "__class__": property(lambda self: spec),
        "__getattr__": __getattr__,
        "__repr__": __repr__,
    }

    # A context-managed spec keeps working: enter to the double itself,
    # exit inert (exceptions are not suppressed). Provided only when
    # the spec defines the protocol, per the no-fabrication rule.

    if hasattr(spec, "__enter__"):
        namespace["__enter__"] = lambda self: self
    if hasattr(spec, "__exit__"):
        namespace["__exit__"] = lambda self, *exc: None

    return namespace


def mock(spec: type) -> Any:
    """Build an instance-shaped double of a class, for the test to
    inject where the code under test expects a collaborator.

    A mock requires a spec and fabricates nothing beyond it. Every
    method of the spec, inherited ones included, becomes a stub that
    mimics the real method: calls are checked against its signature and
    raise TypeError on drift, arguments record by parameter name, and
    the method's kind carries over, so an `async def` method is
    awaited, a generator method is iterated, and the outcome arrives as
    the real one would deliver it. Every method returns None until
    configured with `double.method.returns(...)` or `.raises(...)`;
    there are no fabricated return-value chains, so a call on an
    unconfigured method's result fails loudly instead of inventing an
    object. Wire a graph by configuring one double as another's return
    value.

    The double's surface is exactly the spec's: accessing a name the
    spec does not have raises AttributeError, in the test and in the
    code under test alike, and data attributes hold no fabricated
    values, so the test assigns what the code reads (`double.host =
    "amqp.local"`). `isinstance(double, Spec)` holds, as the double
    stands in for exactly that class; `type(double)` still tells the
    truth. When the spec is a context manager, entering the double
    yields the double and exiting is inert.

    Each method records events under `SpecName.method`, so the normal
    vocabulary applies, `tape.assert_order()` mixes mocked methods with
    real bindings, and a double is a value like a stub: the test places
    it and owns its lifetime. To substitute a class at a location so
    code constructing its own collaborator gets doubles, hold a factory
    in a value binding instead.
    """

    if not isinstance(spec, type):
        raise TypeError(
            f"mock() requires a class as its spec, got {spec!r}: there"
            f" is no spec-less form"
        )

    namespace = _double_namespace(spec)

    for name, attribute in _spec_methods(spec).items():
        if isinstance(attribute, staticmethod):
            # A staticmethod never sees the instance; the descriptor
            # preserves that, handing the stub out unbound.

            namespace[name] = staticmethod(
                stub(f"{spec.__name__}.{name}", mimics=attribute.__func__)
            )
        elif isinstance(attribute, classmethod):
            # The underlying function's cls parameter binds to the
            # double, standing in for the class the real method binds.

            namespace[name] = stub(f"{spec.__name__}.{name}", mimics=attribute.__func__)
        else:
            namespace[name] = stub(f"{spec.__name__}.{name}", mimics=attribute)

    double_class = type(f"mock({spec.__name__})", (object,), namespace)
    return double_class()
