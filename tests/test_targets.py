"""Tests for the kinds of target a binding can wrap.

Lifecycle and behaviour details are covered elsewhere against plain
instance methods; these tests pin that the same machinery works across
the other things a binding can be pointed at: class methods, static
methods, module level functions, classes themselves, and single
instances.
"""

import types
from collections.abc import Callable
from typing import Any

from wrapture import binding


class Gateway:
    def charge(self, amount: int, currency: str = "USD") -> dict[str, Any]:
        return {"id": f"ch_{amount}", "amount": amount}


class Registry:
    @classmethod
    def create(cls, name: str) -> str:
        return f"{cls.__name__}:{name}"

    @staticmethod
    def normalize(name: str) -> str:
        return name.strip().lower()


class Widget:
    def __init__(self, size: int) -> None:
        self.size = size


def _sample_module() -> types.ModuleType:
    """A module object with a function and a class defined in it."""

    def greet(name: str) -> str:
        return f"hello {name}"

    module = types.ModuleType("wrapture_sample")
    vars(module)["greet"] = greet
    vars(module)["Widget"] = Widget
    return module


def test_wrapping_an_instance_method() -> None:
    bnd = binding(Gateway, "charge").on_call.returns({"id": "stub"})

    with bnd:
        assert Gateway().charge(1) == {"id": "stub"}

    assert Gateway().charge(1) == {"id": "ch_1", "amount": 1}


def test_wrapping_a_classmethod() -> None:
    assert binding(Registry, "create").mode == "callable"

    # wrapt hands the class as `instance` for a classmethod, whether the
    # call was made through the class or through an instance.

    seen: list[Any] = []

    def around(
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        seen.append(instance)
        return wrapped(*args, **kwargs)

    bnd = binding(Registry, "create").on_call.decorates(around)

    with bnd:
        assert Registry.create("x") == "Registry:x"
        assert Registry().create("y") == "Registry:y"

    assert seen == [Registry, Registry]
    assert Registry.create("z") == "Registry:z"  # restored


def test_wrapping_a_staticmethod() -> None:
    assert binding(Registry, "normalize").mode == "callable"

    # A staticmethod has no receiver, so `instance` is None.

    seen: list[Any] = []

    def around(
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        seen.append(instance)
        return wrapped(*args, **kwargs)

    bnd = binding(Registry, "normalize").on_call.decorates(around)

    with bnd:
        assert Registry.normalize("  Widget  ") == "widget"
        assert Registry().normalize("GIZMO") == "gizmo"

    assert seen == [None, None]
    assert Registry.normalize("  A ") == "a"  # restored


def test_wrapping_a_module_level_function() -> None:
    module = _sample_module()
    assert binding(module, "greet").mode == "callable"

    bnd = binding(module, "greet").on_call.transforms_result(str.upper)

    with bnd:
        assert module.greet("world") == "HELLO WORLD"

    assert module.greet("world") == "hello world"  # restored


def test_wrapping_a_class_type_definition() -> None:
    # A class stored as a module attribute is itself callable, so wrapping
    # it intercepts instantiation.

    module = _sample_module()
    assert binding(module, "Widget").mode == "callable"

    bnd = binding(module, "Widget").on_call.transforms_args(
        lambda args, kwargs: ((args[0] * 2,), kwargs)
    )

    with bnd:
        widget = module.Widget(3)
        assert isinstance(widget, Widget)
        assert widget.size == 6

    assert vars(module)["Widget"] is Widget  # restored, not a wrapper
    assert module.Widget(3).size == 3


def test_wrapping_on_an_instance_target_affects_only_that_instance() -> None:
    patched = Gateway()
    untouched = Gateway()

    bnd = binding(patched, "charge").on_call.returns({"id": "stub"})

    with bnd:
        assert patched.charge(1) == {"id": "stub"}
        assert untouched.charge(1) == {"id": "ch_1", "amount": 1}

    assert patched.charge(1) == {"id": "ch_1", "amount": 1}  # restored
