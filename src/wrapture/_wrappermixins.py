"""Composable introspection-override mixins for wrapt function wrappers.

A wrapt FunctionWrapper delegates all introspection to the callable it
wraps: `inspect.signature()`, the calling-convention probes
(`iscoroutinefunction()` and friends), `__annotations__` and the rest
all report what the wrapped callable says. These mixins let a
FunctionWrapper subclass override either axis for the wrapper itself,
without touching the wrapped callable:

- SignatureOverrideMixin / BoundSignatureOverrideMixin: report a given
  `inspect.Signature` (or the signature of a prototype callable) from
  `__signature__`, deriving `__annotations__`, `__defaults__`,
  `__kwdefaults__` and the argument-related `__code__` attributes to
  match, including the bound-method case where `self` is stripped.
- ConventionOverrideMixin / BoundConventionOverrideMixin: report a
  named calling convention ("sync", "generator", "coroutine" or
  "async_generator") by adjusting the flags the wrapper's `__code__`
  exposes, so the `inspect` probes answer the override.

Each option defaults to None, meaning delegate exactly as before, so
listing the mixins costs nothing until an override is passed. Usage:

    class BoundStub(BoundConventionOverrideMixin,
                    BoundSignatureOverrideMixin,
                    BoundFunctionWrapper):
        pass

    class Stub(ConventionOverrideMixin,
               SignatureOverrideMixin,
               FunctionWrapper):
        __bound_function_wrapper__ = BoundStub

    stub = Stub(template, wrapper_fn,
                signature=prototype, convention="coroutine")

The mixins consume their keyword in a cooperative __init__ and pass
everything else up, so the host class chains `super().__init__` as
usual. The convention mixin goes outside the signature mixin (earlier
in the bases) as above: each layers its view over the next one's
`__code__`, so that order applies convention flags to signature-derived
argument attributes. Bound wrappers read the state from their parent
wrapper, so `__get__` needs no extra plumbing.

The module is self-contained over wrapt's public wrapper classes, so
the implementation can move into wrapt itself unchanged.
"""

from __future__ import annotations

import inspect
from inspect import (
    CO_ASYNC_GENERATOR,
    CO_COROUTINE,
    CO_GENERATOR,
    CO_ITERABLE_COROUTINE,
    CO_VARARGS,
    CO_VARKEYWORDS,
    Parameter,
    Signature,
)
from typing import Any

from wrapt import CallableObjectProxy, ObjectProxy

CONVENTIONS = ("sync", "generator", "coroutine", "async_generator")

# The code-object flag bits every convention override starts by
# clearing; the convention then asserts the bits it stands for.

_CONVENTION_BITS = (
    CO_GENERATOR | CO_COROUTINE | CO_ITERABLE_COROUTINE | CO_ASYNC_GENERATOR
)

_CONVENTION_FLAGS = {
    "sync": 0,
    "generator": CO_GENERATOR,
    "coroutine": CO_COROUTINE,
    "async_generator": CO_ASYNC_GENERATOR,
}


def _resolve_signature(signature: Any) -> Signature | None:
    """Accept a Signature, a prototype callable, or None."""

    if signature is None or isinstance(signature, Signature):
        return signature

    return inspect.signature(signature)


def _check_convention(convention: str | None) -> str | None:
    if convention is not None and convention not in CONVENTIONS:
        raise ValueError(
            f"convention must be one of {', '.join(CONVENTIONS)} or None,"
            f" got {convention!r}"
        )

    return convention


# ---------------------------------------------------------------------------
# code-object proxies: each overrides one axis and falls through the rest
# ---------------------------------------------------------------------------


class _SignatureCode(ObjectProxy[Any]):
    """A code-object proxy deriving the argument-related attributes from
    a Signature; everything else falls through to the real code object.
    """

    def __init__(self, wrapped: Any, signature: Signature) -> None:
        super().__init__(wrapped)

        self._self_signature = signature

    @property
    def co_argcount(self) -> int:
        kinds = (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        return sum(
            parameter.kind in kinds
            for parameter in self._self_signature.parameters.values()
        )

    @property
    def co_posonlyargcount(self) -> int:
        return sum(
            parameter.kind is Parameter.POSITIONAL_ONLY
            for parameter in self._self_signature.parameters.values()
        )

    @property
    def co_kwonlyargcount(self) -> int:
        return sum(
            parameter.kind is Parameter.KEYWORD_ONLY
            for parameter in self._self_signature.parameters.values()
        )

    @property
    def co_varnames(self) -> tuple[str, ...]:
        positional: list[str] = []
        keyword_only: list[str] = []
        var_positional: str | None = None
        var_keyword: str | None = None

        for parameter in self._self_signature.parameters.values():
            if parameter.kind in (
                Parameter.POSITIONAL_ONLY,
                Parameter.POSITIONAL_OR_KEYWORD,
            ):
                positional.append(parameter.name)
            elif parameter.kind is Parameter.KEYWORD_ONLY:
                keyword_only.append(parameter.name)
            elif parameter.kind is Parameter.VAR_POSITIONAL:
                var_positional = parameter.name
            elif parameter.kind is Parameter.VAR_KEYWORD:
                var_keyword = parameter.name

        names = positional + keyword_only
        if var_positional is not None:
            names.append(var_positional)
        if var_keyword is not None:
            names.append(var_keyword)

        return tuple(names)

    @property
    def co_flags(self) -> int:
        kinds = {
            parameter.kind for parameter in self._self_signature.parameters.values()
        }

        flags = int(self.__wrapped__.co_flags) & ~(CO_VARARGS | CO_VARKEYWORDS)
        if Parameter.VAR_POSITIONAL in kinds:
            flags |= CO_VARARGS
        if Parameter.VAR_KEYWORD in kinds:
            flags |= CO_VARKEYWORDS

        return flags


class _ConventionCode(ObjectProxy[Any]):
    """A code-object proxy asserting a calling convention in co_flags;
    everything else falls through to the real code object."""

    def __init__(self, wrapped: Any, convention: str) -> None:
        super().__init__(wrapped)

        self._self_convention = convention

    @property
    def co_flags(self) -> int:
        flags = int(self.__wrapped__.co_flags) & ~_CONVENTION_BITS
        return flags | _CONVENTION_FLAGS[self._self_convention]


# ---------------------------------------------------------------------------
# function surrogates: what a bound wrapper hands out as __func__
# ---------------------------------------------------------------------------


class _SignatureFunctionSurrogate(CallableObjectProxy[Any]):
    """A function proxy exposing an overridden signature.

    Handed out as a bound wrapper's __func__ so that inspect.signature,
    which treats the bound wrapper as a method and consults __func__,
    sees the override and strips self/cls as it would for a real
    method.
    """

    def __init__(self, wrapped: Any, signature: Signature) -> None:
        super().__init__(wrapped)

        self._self_signature = signature

    @property
    def __signature__(self) -> Signature:
        return self._self_signature

    @property
    def __code__(self) -> Any:
        return _SignatureCode(self.__wrapped__.__code__, self._self_signature)


class _ConventionFunctionSurrogate(CallableObjectProxy[Any]):
    """A function proxy exposing an overridden calling convention."""

    def __init__(self, wrapped: Any, convention: str) -> None:
        super().__init__(wrapped)

        self._self_convention = convention

    @property
    def __code__(self) -> Any:
        return _ConventionCode(self.__wrapped__.__code__, self._self_convention)


# ---------------------------------------------------------------------------
# the mixins
# ---------------------------------------------------------------------------


def _annotations_of(signature: Signature) -> dict[str, Any]:
    annotations = {
        parameter.name: parameter.annotation
        for parameter in signature.parameters.values()
        if parameter.annotation is not Parameter.empty
    }

    if signature.return_annotation is not Signature.empty:
        annotations["return"] = signature.return_annotation

    return annotations


def _defaults_of(signature: Signature) -> tuple[Any, ...] | None:
    defaults = tuple(
        parameter.default
        for parameter in signature.parameters.values()
        if parameter.kind
        in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        and parameter.default is not Parameter.empty
    )

    return defaults or None


def _kwdefaults_of(signature: Signature) -> dict[str, Any] | None:
    kwdefaults = {
        parameter.name: parameter.default
        for parameter in signature.parameters.values()
        if parameter.kind is Parameter.KEYWORD_ONLY
        and parameter.default is not Parameter.empty
    }

    return kwdefaults or None


class SignatureOverrideMixin:
    """Report a chosen signature from a FunctionWrapper subclass.

    Consumes the keyword-only `signature` at construction: an
    inspect.Signature, a prototype callable whose signature is taken,
    or None to delegate to the wrapped callable exactly as without the
    mixin. With an override in place, `__signature__`,
    `__annotations__`, `__defaults__`, `__kwdefaults__` and the
    argument-related `__code__` attributes all answer from it.
    """

    def __init__(self, *args: Any, signature: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self._self_signature = _resolve_signature(signature)

    @property
    def __signature__(self) -> Signature:
        signature = self._self_signature
        if signature is None:
            raise AttributeError("__signature__")

        return signature

    @property
    def __annotations__(self) -> dict[str, Any]:  # type: ignore[override]
        signature = self._self_signature
        if signature is None:
            raise AttributeError("__annotations__")

        return _annotations_of(signature)

    @property
    def __defaults__(self) -> tuple[Any, ...] | None:
        signature = self._self_signature
        if signature is None:
            raise AttributeError("__defaults__")

        return _defaults_of(signature)

    @property
    def __kwdefaults__(self) -> dict[str, Any] | None:
        signature = self._self_signature
        if signature is None:
            raise AttributeError("__kwdefaults__")

        return _kwdefaults_of(signature)

    @property
    def __code__(self) -> Any:
        # Layer over whatever the next class in the MRO reports, so
        # several overrides compose; the base classes define no
        # __code__ descriptor, so the fallback is the wrapped
        # callable's own code object.

        try:
            code = super().__code__  # type: ignore[misc]
        except AttributeError:
            code = self.__wrapped__.__code__  # type: ignore[attr-defined]

        signature = self._self_signature
        if signature is None:
            return code

        return _SignatureCode(code, signature)


class BoundSignatureOverrideMixin:
    """The bound-wrapper side of SignatureOverrideMixin.

    Reads the override from the parent wrapper, and hands out a
    __func__ surrogate carrying it so inspect.signature on the bound
    wrapper strips self/cls exactly as for a real method.
    """

    @property
    def __signature__(self) -> Signature:
        signature: Signature | None = self._self_parent._self_signature  # type: ignore[attr-defined]
        if signature is None:
            raise AttributeError("__signature__")

        return signature

    @property
    def __func__(self) -> Any:
        signature = self._self_parent._self_signature  # type: ignore[attr-defined]
        if signature is None:
            raise AttributeError("__func__")

        wrapped = self.__wrapped__  # type: ignore[attr-defined]
        function = getattr(wrapped, "__func__", wrapped)

        return _SignatureFunctionSurrogate(function, signature)


class ConventionOverrideMixin:
    """Report a chosen calling convention from a FunctionWrapper
    subclass.

    Consumes the keyword-only `convention` at construction: "sync",
    "generator", "coroutine", "async_generator", or None to delegate to
    the wrapped callable exactly as without the mixin. With an override
    in place the wrapper's `__code__` asserts the matching flags, so
    `inspect.iscoroutinefunction()`, `isasyncgenfunction()` and
    `isgeneratorfunction()` answer the override; the sync-direction
    conventions also set `_self_is_not_coroutine`, the authoritative
    marker wrapt's own convention checks respect through wrapper
    chains.
    """

    def __init__(
        self, *args: Any, convention: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)

        self._self_convention = _check_convention(convention)
        if convention in ("sync", "generator"):
            self._self_is_not_coroutine = True

    @property
    def __code__(self) -> Any:
        try:
            code = super().__code__  # type: ignore[misc]
        except AttributeError:
            code = self.__wrapped__.__code__  # type: ignore[attr-defined]

        convention = self._self_convention
        if convention is None:
            return code

        return _ConventionCode(code, convention)


class BoundConventionOverrideMixin:
    """The bound-wrapper side of ConventionOverrideMixin.

    Reads the override from the parent wrapper, and hands out a
    __func__ surrogate carrying it, layered over any surrogate the next
    mixin in the MRO provides so several overrides compose. With no
    override of its own it passes the next layer's surrogate through,
    and raises AttributeError only when no layer has one, which lets
    attribute delegation take over as if the property were absent.
    """

    @property
    def __func__(self) -> Any:
        convention = self._self_parent._self_convention  # type: ignore[attr-defined]

        try:
            function = super().__func__  # type: ignore[misc]
        except AttributeError:
            if convention is None:
                raise

            wrapped = self.__wrapped__  # type: ignore[attr-defined]
            function = getattr(wrapped, "__func__", wrapped)

        if convention is None:
            return function

        return _ConventionFunctionSurrogate(function, convention)
