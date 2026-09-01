"""Decorator forms of bindings and timelines: bound() and taped().

The context manager is wrapture's primary application form, and the
sharpest one when a recording scope should cover only part of a test.
These decorators cover the other common shape: a test that binds one or
two statically addressable targets around its whole body. `bound()`
takes the same addressing arguments as `binding()` and mirrors its
fluent chain; `taped()` opens a `timeline()` around the call. Both
inject what they build as a keyword argument, the way a pytest fixture
would, and remove it afterwards.

Read a stack of these decorators as a top-to-bottom sequence of
statements about the function below: stages accumulate in reading
order, and where two statements set the same terminal, the last one
reading down wins, exactly as the same statements would behave on a
live binding.
"""

from __future__ import annotations

import inspect
import keyword
from collections.abc import Callable, Iterable
from typing import Any, cast

from wrapt import MISSING, FunctionWrapper

from .bindings import Binding, binding
from .capture import CapturePolicy
from .exceptions import ExpectationNotMetError, WrongModeError
from .timeline import _Appliable, _current_tape, timeline

__all__ = ["BoundSpec", "bound", "taped"]


# The single-phase verbs each channel recorder accepts, matching the
# behaviour namespaces in behaviours.py. then() and advance() are
# deliberately absent: phases are the test's script and are configured
# in the body through the injected binding.

_CHANNEL_VERBS: dict[str, frozenset[str]] = {
    "on_call": frozenset(
        {
            "returns",
            "returns_from",
            "raises",
            "decorates",
            "transforms_args",
            "transforms_result",
            "validates_args",
            "validates_result",
            "passes_through",
        }
    ),
    "on_get": frozenset(
        {
            "returns",
            "returns_from",
            "raises",
            "decorates",
            "transforms",
            "validates",
            "passes_through",
        }
    ),
    "on_set": frozenset(
        {"rejects", "raises", "decorates", "transforms", "validates", "passes_through"}
    ),
    "on_delete": frozenset(
        {"rejects", "raises", "decorates", "validates", "passes_through"}
    ),
}

# What each addressing shape statically implies about the binding's
# mode, before any target is resolved. "unknown" means positional
# addressing whose callable-versus-attribute split is decided when the
# binding is built; both channel families are allowed and a wrong one
# fails at first call exactly as it would on a with-block binding.

_KIND_CHANNELS: dict[str, frozenset[str]] = {
    "callable": frozenset({"on_call"}),
    "attribute": frozenset({"on_get", "on_set", "on_delete"}),
    "unknown": frozenset(_CHANNEL_VERBS),
    "value": frozenset(),
    "mapping": frozenset(),
}

# Expectation declarations mirror Binding.expect_*: they live on the
# root spec (an expectation is a property of the binding, not of one
# channel) and are also reachable through a channel recorder, exactly
# as a real namespace delegates them to its binding.

_EXPECT_VERBS = frozenset(
    {"expect_times", "expect_once", "expect_never", "expect_at_least"}
)

_KIND_VALUE_VERBS: dict[str, frozenset[str]] = {
    "value": frozenset({"overrides", "hides", "passes_through"}),
    "mapping": frozenset({"overrides", "updates", "passes_through"}),
    "callable": frozenset(),
    "attribute": frozenset(),
    "unknown": frozenset(),
}


def _spec_kind(mode: str | None, attr: str | None, item: Any) -> str:
    """The statically knowable mode of the binding a spec will build."""

    if mode is not None:
        return mode

    if attr is not None or item is not MISSING:
        return "value"

    return "unknown"


def _layers(fn: Any) -> Iterable[Any]:
    """The decorator layers of `fn`, outermost first, following
    __wrapped__ through foreign decorators."""

    seen: set[int] = set()
    current = fn

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "__wrapped__", None)


def _claimed_aliases(fn: Any) -> tuple[str, ...]:
    """The aliases already injected by wrapture decorator layers of `fn`."""

    for layer in _layers(fn):
        claimed = getattr(layer, "_self_wrapture_aliases", None)
        if claimed is not None:
            return tuple(claimed)

    return ()


def _check_convention(fn: Any, decorator: str) -> bool:
    """Reject conventions the decorators cannot span; True if coroutine.

    Generator and async-generator functions only occur as fixtures, and
    a decorator applied around the call would remove its binding before
    the body ever runs; the with-block inside the fixture is the form
    for those. A pytest fixture marker below the decorator is the same
    mistake caught earlier.
    """

    is_fixture = (
        getattr(fn, "_pytestfixturefunction", None) is not None
        or getattr(fn, "_fixture_function_marker", None) is not None
    )
    if is_fixture:
        raise TypeError(
            f"{decorator} cannot decorate a pytest fixture: the binding"
            " would not span the tests that use it. Use a with block"
            " around the fixture's yield instead."
        )

    if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
        raise TypeError(
            f"{decorator} cannot decorate a generator function: the"
            " decorated call would end before the body runs. For a"
            " fixture, use a with block around the yield instead."
        )

    return inspect.iscoroutinefunction(fn)


def _pruned_signature(fn: Any, alias: str, decorator: str) -> inspect.Signature | None:
    """The visible signature for the wrapper: `fn`'s minus `alias`.

    None when `fn` takes the injection through **kwargs, so nothing
    needs pruning. A function with neither a matching parameter nor
    **kwargs cannot receive the injection, and that is an error now
    rather than a TypeError at first call.
    """

    signature = inspect.signature(fn)
    parameters = signature.parameters

    if alias in parameters:
        return signature.replace(
            parameters=[p for name, p in parameters.items() if name != alias]
        )

    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return None

    raise TypeError(
        f"{decorator} injects {alias!r} but the decorated function has no"
        f" parameter of that name and no **kwargs; add the parameter, or"
        f" pick another name with alias=."
    )


def _verify_expectations(built: Binding) -> None:
    """Verify a decorator binding's declared expectations at teardown.

    Called only when the body did not raise, mirroring timeline exit:
    a verification error on top of an in-flight failure would bury it.
    Expectations with nothing recording are a loud error, not a silent
    pass, exactly as reading `events` outside a timeline is.
    """

    if not built._expectations:
        return

    tape = _current_tape()
    if tape is None:
        raise ExpectationNotMetError(
            f"{built.label}: expectations were declared but nothing was"
            " recording; open a tape with taped() outermost, the pytest"
            " plugin's tape fixture, or a timeline() spanning the test"
        )

    built._verify(tape)


def _finish_wrapper(
    wrapper: Any,
    fn: Any,
    alias: str,
    pruned: inspect.Signature | None,
    inner: tuple[str, ...],
) -> Any:
    """Stamp the wrapper's visible signature and its claimed aliases."""

    if pruned is not None:
        # Assigning through the proxy lands on the innermost function,
        # which is exactly the object inspect.signature() resolves to
        # through the __wrapped__ chain; each layer subtracts only its
        # own alias, so the outermost view has all of them removed.

        wrapper.__signature__ = pruned

    wrapper._self_wrapture_aliases = (*inner, alias)

    return wrapper


class _BoundChannel:
    """Recorder for one channel of a BoundSpec's chain.

    Verb calls are recorded for replay onto each fresh binding, and the
    recorder hands itself back, the chain continuation rule. The
    recorder is also decorator-callable, since a chain may end on a
    channel verb.
    """

    __slots__ = ("_channel", "_spec")

    def __init__(self, spec: BoundSpec, channel: str) -> None:
        self._spec = spec
        self._channel = channel

    def __getattr__(self, name: str) -> Callable[..., _BoundChannel]:
        if name in ("then", "advance"):
            raise TypeError(
                f"{name}() is not available in a bound() chain: phases are"
                " the test's script and are configured in the body, through"
                " the injected binding."
            )

        # Expectations route to the root spec, as a real namespace
        # delegates them to its binding. The cast papers over the root
        # being handed back instead of the recorder; both are
        # decorator-callable and carry the same chain surface.

        if name in _EXPECT_VERBS:
            return cast("Callable[..., _BoundChannel]", getattr(self._spec, name))

        if name not in _CHANNEL_VERBS[self._channel]:
            raise AttributeError(f"bound() chain: {self._channel} has no verb {name!r}")

        def record(*args: Any, **kwargs: Any) -> _BoundChannel:
            self._spec._program.append((self._channel, name, args, kwargs))
            return self

        record.__name__ = name

        return record

    def __call__(self, fn: Any) -> Any:
        return self._spec(fn)

    def __repr__(self) -> str:
        return f"<bound {self._channel} recorder of {self._spec!r}>"


class BoundSpec:
    """The recorded form of a binding, applied by decoration.

    Created by bound(). Holds the addressing arguments and the verb
    program; each call of the decorated function constructs a fresh
    binding, replays the program onto it, applies it around the call,
    and injects it as a keyword argument.
    """

    def __init__(
        self,
        target: Any,
        attrs: tuple[str, ...],
        alias: str | None,
        kwargs: dict[str, Any],
    ) -> None:
        self._target = target
        self._attrs = attrs
        self._alias = alias
        self._kwargs = kwargs
        self._kind = _spec_kind(
            kwargs.get("mode"), kwargs.get("attr"), kwargs.get("item", MISSING)
        )
        self._program: list[
            tuple[str | None, str, tuple[Any, ...], dict[str, Any]]
        ] = []

    # -- the chain --------------------------------------------------------

    def _channel(self, name: str) -> _BoundChannel:
        if name not in _KIND_CHANNELS[self._kind]:
            raise WrongModeError(
                f"bound() chain: {name} is not available on a {self._kind!r} binding"
            )

        return _BoundChannel(self, name)

    @property
    def on_call(self) -> _BoundChannel:
        """The call channel of the chain."""

        return self._channel("on_call")

    @property
    def on_get(self) -> _BoundChannel:
        """The attribute-read channel of the chain."""

        return self._channel("on_get")

    @property
    def on_set(self) -> _BoundChannel:
        """The attribute-write channel of the chain."""

        return self._channel("on_set")

    @property
    def on_delete(self) -> _BoundChannel:
        """The attribute-delete channel of the chain."""

        return self._channel("on_delete")

    def _value_verb(self, name: str, *args: Any) -> BoundSpec:
        if name not in _KIND_VALUE_VERBS[self._kind]:
            raise WrongModeError(
                f"bound() chain: {name}() is only available on a value or"
                f" mapping binding (a slot named with attr= or item=, or"
                f" mode='mapping'); this is a {self._kind!r} binding"
            )

        self._program.append((None, name, args, {}))

        return self

    def overrides(self, value: Any) -> BoundSpec:
        """Hold `value` in the slot, or as a mapping's whole content,
        while the binding is applied. Value and mapping bindings only."""

        return self._value_verb("overrides", value)

    def updates(self, values: Any) -> BoundSpec:
        """Merge `values` over the mapping's content while applied.
        Mapping bindings only."""

        return self._value_verb("updates", values)

    def hides(self) -> BoundSpec:
        """Keep the slot absent while applied. Value bindings only."""

        return self._value_verb("hides")

    def passes_through(self) -> BoundSpec:
        """Leave the slot or content as it really is. Value and mapping
        bindings only; on a channel, use the channel's own
        passes_through()."""

        return self._value_verb("passes_through")

    # -- declared expectations --------------------------------------------

    def _expect_verb(self, name: str, *args: Any) -> BoundSpec:
        # Value and mapping bindings record nothing, exactly the check
        # Binding._expects makes; failing here keeps it at decoration.

        if self._kind in ("value", "mapping"):
            raise WrongModeError(
                f"bound() chain: a {self._kind!r} binding records nothing;"
                f" it cannot carry an expectation"
            )

        self._program.append((None, name, args, {}))

        return self

    def expect_times(self, count: int) -> BoundSpec:
        """Declare that the binding records exactly `count` events,
        verified when the decorator removes it after a passing body."""

        return self._expect_verb("expect_times", count)

    def expect_once(self) -> BoundSpec:
        """Declare that the binding records exactly one event."""

        return self._expect_verb("expect_once")

    def expect_never(self) -> BoundSpec:
        """Declare that the binding records no events."""

        return self._expect_verb("expect_never")

    def expect_at_least(self, count: int) -> BoundSpec:
        """Declare that the binding records at least `count` events."""

        return self._expect_verb("expect_at_least", count)

    # -- alias derivation -------------------------------------------------

    def _derived_alias(self) -> str | None:
        attr = self._kwargs.get("attr")
        item = self._kwargs.get("item", MISSING)

        if attr is not None:
            name: Any = attr
        elif item is not MISSING:
            name = item
        elif self._attrs:
            name = ".".join(self._attrs).rsplit(".", 1)[-1]
        elif isinstance(self._target, str):
            name = self._target.rsplit(".", 1)[-1]
        else:
            name = None

        if (
            not isinstance(name, str)
            or not name.isidentifier()
            or keyword.iskeyword(name)
        ):
            return None

        return name

    def _resolve_alias(self) -> str:
        if self._alias is not None:
            if not self._alias.isidentifier() or keyword.iskeyword(self._alias):
                raise TypeError(
                    f"bound(): alias {self._alias!r} is not a valid Python identifier"
                )

            return self._alias

        derived = self._derived_alias()
        if derived is None:
            raise TypeError(
                "bound(): no injectable name can be derived from this"
                " addressing (the slot name is not a valid identifier);"
                " pass alias= to name the injected argument."
            )

        return derived

    # -- collapse ---------------------------------------------------------

    def _same_addressing(self, other: BoundSpec) -> bool:
        targets_equal = self._target is other._target or (
            isinstance(self._target, str)
            and isinstance(other._target, str)
            and self._target == other._target
        )

        return (
            targets_equal
            and self._attrs == other._attrs
            and self._kwargs == other._kwargs
        )

    # -- construction and decoration --------------------------------------

    def _build(self) -> Binding:
        """A fresh binding with the recorded program replayed onto it."""

        built = binding(self._target, *self._attrs, **self._kwargs)

        for channel, verb, args, kwargs in self._program:
            owner: Any = getattr(built, channel) if channel else built
            getattr(owner, verb)(*args, **kwargs)

        return built

    def __call__(self, fn: Any) -> Any:
        is_coroutine = _check_convention(fn, "bound()")
        alias = self._resolve_alias()

        # Collapse onto an existing layer addressing the same slot under
        # the same alias: merge this decorator's program in front of the
        # already-recorded one (this decorator is textually earlier, and
        # replay in reading order is what makes the stack behave as the
        # same statements written top to bottom) and add no new layer.

        for layer in _layers(fn):
            other = getattr(layer, "_self_wrapture_spec", None)
            if (
                other is not None
                and self._same_addressing(other)
                and getattr(layer, "_self_wrapture_alias", None) == alias
            ):
                other._program[:0] = self._program
                return fn

        inner = _claimed_aliases(fn)
        if alias in inner:
            raise TypeError(
                f"bound(): the alias {alias!r} is already injected by"
                " another decorator on this function; pass alias= on one"
                " of them."
            )

        pruned = _pruned_signature(fn, alias, "bound()")
        spec = self

        if is_coroutine:

            def invoke(
                wrapped: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
            ) -> Any:
                async def run() -> Any:
                    built = spec._build().apply()
                    try:
                        result = await wrapped(*args, **{**kwargs, alias: built})
                    finally:
                        built.remove()

                    _verify_expectations(built)
                    return result

                return run()

        else:

            def invoke(
                wrapped: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
            ) -> Any:
                built = spec._build().apply()
                try:
                    result = wrapped(*args, **{**kwargs, alias: built})
                finally:
                    built.remove()

                _verify_expectations(built)
                return result

        def wrapper(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return invoke(wrapped, args, kwargs)

        decorated: Any = FunctionWrapper(fn, wrapper)
        decorated._self_wrapture_spec = self
        decorated._self_wrapture_alias = alias

        return _finish_wrapper(decorated, fn, alias, pruned, inner)

    def __repr__(self) -> str:
        label = self._kwargs.get("label") or self._derived_alias() or "?"
        return f"<BoundSpec {label!r} {self._kind}>"


def bound(
    target: Any,
    *attrs: str,
    alias: str | None = None,
    label: str | None = None,
    mode: str | None = None,
    missing_ok: bool = False,
    capture: CapturePolicy | str | None = None,
    capture_args: CapturePolicy | str | None = None,
    capture_result: CapturePolicy | str | None = None,
    stack: int | str | None = None,
    when: Callable[[Any, tuple[Any, ...], dict[str, Any]], Any] | bool | None = None,
    tree: bool = False,
    leaf: bool = False,
    category: str | None = None,
    strict: bool = True,
    attr: str | None = None,
    item: Any = MISSING,
) -> BoundSpec:
    """A binding applied by decoration, addressed exactly as binding().

    The result mirrors the binding's fluent chain for one phase's worth
    of behaviour and is itself the decorator:

        @wrapture.bound(Gateway, "charge").on_call.returns(None)
        def test_order_is_not_charged_twice(charge):
            ...

    Each call of the decorated function constructs a fresh binding,
    replays the chain's configuration onto it, applies it around the
    call (spanning the await for an async test), injects it as a
    keyword argument named after the slot (or `alias`), and removes it
    afterwards. Phases are not part of the chain; configure them in the
    body through the injected binding.

    Stacked bound() decorators addressing the same slot collapse into
    one binding, configured as the same statements written top to
    bottom.
    """

    kwargs: dict[str, Any] = {
        "label": label,
        "mode": mode,
        "missing_ok": missing_ok,
        "capture": capture,
        "capture_args": capture_args,
        "capture_result": capture_result,
        "stack": stack,
        "when": when,
        "tree": tree,
        "leaf": leaf,
        "category": category,
        "strict": strict,
        "attr": attr,
        "item": item,
    }

    return BoundSpec(target, attrs, alias, kwargs)


def taped(
    *bindings: _Appliable | Iterable[_Appliable],
    alias: str = "tape",
) -> Callable[[Any], Any]:
    """Open a timeline around each call of the decorated function.

    The tape is injected as a keyword argument (named `alias`,
    "tape" by default, matching the pytest plugin's fixture), so the
    decorator and fixture spellings of a test body read identically.
    Bindings passed here are applied on entry and removed on exit,
    exactly as with timeline(); bindings applied by bound() decorators
    or inside the body record onto the tape without being named.

    Under the plugin's ambient tape this nests, exactly as a
    timeline() block would; the injected handle is the nested tape.
    """

    if not alias.isidentifier() or keyword.iskeyword(alias):
        raise TypeError(f"taped(): alias {alias!r} is not a valid Python identifier")

    applied = tuple(bindings)

    def decorate(fn: Any) -> Any:
        is_coroutine = _check_convention(fn, "taped()")

        inner = _claimed_aliases(fn)
        if alias in inner:
            raise TypeError(
                f"taped(): the alias {alias!r} is already injected by"
                " another decorator on this function; pass alias= on one"
                " of them."
            )

        pruned = _pruned_signature(fn, alias, "taped()")

        if is_coroutine:

            def invoke(
                wrapped: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
            ) -> Any:
                async def run() -> Any:
                    with timeline(*applied) as tape:
                        return await wrapped(*args, **{**kwargs, alias: tape})

                return run()

        else:

            def invoke(
                wrapped: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
            ) -> Any:
                with timeline(*applied) as tape:
                    return wrapped(*args, **{**kwargs, alias: tape})

        def wrapper(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            return invoke(wrapped, args, kwargs)

        decorated = FunctionWrapper(fn, wrapper)

        return _finish_wrapper(decorated, fn, alias, pruned, inner)

    return decorate
