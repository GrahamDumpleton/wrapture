"""Tests for attribute-mode bindings whose owner is a module."""

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

import wrapture
from wrapture import binding
from wrapture.attributes import _module_class


def _swap_class(module: Any, cls: type) -> None:
    """Assign a module's __class__ the way a lazy-import shim would."""

    module.__class__ = cls


@pytest.fixture
def config() -> Iterator[Any]:
    """A throwaway module registered in sys.modules, so string targets
    resolve to it, removed again afterwards. Typed Any so attribute
    access on it is unchecked."""

    module: Any = types.ModuleType("wrapture_modattr_config")
    vars(module)["TIMEOUT"] = 30
    vars(module)["RETRIES"] = 3
    sys.modules[module.__name__] = module

    try:
        yield module
    finally:
        sys.modules.pop(module.__name__, None)

    assert type(module) is types.ModuleType


# ---------------------------------------------------------------------------
# detection, labels and passthrough
# ---------------------------------------------------------------------------


def test_module_data_is_attribute_mode(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")

    assert timeout.mode == "attribute"
    assert timeout.label == "wrapture_modattr_config.TIMEOUT"
    assert timeout.path == "wrapture_modattr_config:TIMEOUT"


def test_string_target_resolves_module(config: Any) -> None:
    timeout = binding("wrapture_modattr_config", "TIMEOUT")

    with timeout:
        assert timeout.active
        assert config.TIMEOUT == 30


def test_passthrough_leaves_value_and_type(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")

    with timeout:
        assert type(config) is not types.ModuleType
        assert issubclass(type(config), types.ModuleType)
        assert type(config).__name__ == "module"

        assert config.TIMEOUT == 30
        config.TIMEOUT = 40
        assert config.TIMEOUT == 40
        assert vars(config)["TIMEOUT"] == 40

        del config.TIMEOUT
        assert not hasattr(config, "TIMEOUT")
        config.TIMEOUT = 30

    assert type(config) is types.ModuleType
    assert config.TIMEOUT == 30


def test_other_attributes_are_untouched(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")

    with timeout:
        assert "TIMEOUT" in vars(type(config))
        assert "RETRIES" not in vars(type(config))
        assert config.RETRIES == 3
        assert config.__name__ == "wrapture_modattr_config"


# ---------------------------------------------------------------------------
# behaviour
# ---------------------------------------------------------------------------


def test_get_returns_and_phases(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")
    timeout.on_get.returns(5)
    timeout.on_get.then(after=2).passes_through()

    with timeout:
        assert config.TIMEOUT == 5
        assert config.TIMEOUT == 5
        assert config.TIMEOUT == 30


def test_get_returns_from(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")
    timeout.on_get.returns_from(iter([1, 2]))
    timeout.on_get.then().passes_through()

    with timeout:
        assert [config.TIMEOUT for _ in range(3)] == [1, 2, 30]


def test_set_transforms_and_raises(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")
    timeout.on_set.transforms(lambda value: value * 2)

    with timeout:
        config.TIMEOUT = 10
        assert config.TIMEOUT == 20

    config.TIMEOUT = 30

    frozen = binding(config, "TIMEOUT")
    frozen.on_set.raises(RuntimeError("frozen"))

    with frozen:
        with pytest.raises(RuntimeError, match="frozen"):
            config.TIMEOUT = 1
        assert config.TIMEOUT == 30


def test_delete_raises(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")
    timeout.on_delete.raises(PermissionError("keep it"))

    with timeout:
        with pytest.raises(PermissionError):
            del config.TIMEOUT

    assert config.TIMEOUT == 30


def test_suspend_and_resume(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")
    timeout.on_get.returns(5)

    with timeout:
        timeout.suspend()
        assert config.TIMEOUT == 30
        assert timeout.active
        timeout.resume()
        assert config.TIMEOUT == 5


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def test_events_carry_module_as_instance(config: Any) -> None:
    timeout = binding(config, "TIMEOUT")

    with timeout, wrapture.timeline() as tape:
        _ = config.TIMEOUT
        config.TIMEOUT = 31
        del config.TIMEOUT
        config.TIMEOUT = 30

    events = tape.for_binding(timeout)
    assert [event.kind for event in events] == ["get", "set", "delete", "set"]
    assert all(event.instance is config for event in events)
    assert events[0].label == "wrapture_modattr_config.TIMEOUT"
    assert events[0].result == 30
    assert events[1].value == 31
    assert events[1].previous == 30
    assert events[2].previous == 31


# ---------------------------------------------------------------------------
# sharing one class across bindings
# ---------------------------------------------------------------------------


def test_two_names_share_one_class(config: Any) -> None:
    timeout = binding(config, "TIMEOUT").apply()
    cls = type(config)

    retries = binding(config, "RETRIES").apply()
    assert type(config) is cls
    assert "TIMEOUT" in vars(cls) and "RETRIES" in vars(cls)

    timeout.remove()
    assert type(config) is cls
    assert "TIMEOUT" not in vars(cls)
    assert config.RETRIES == 3

    retries.remove()
    assert type(config) is types.ModuleType


def test_two_bindings_on_one_name_compose(config: Any) -> None:
    outer = binding(config, "TIMEOUT")
    inner = binding(config, "TIMEOUT")
    inner.on_get.returns(1)
    outer.on_get.transforms(lambda value: value + 1)

    inner.apply()
    outer.apply()
    assert config.TIMEOUT == 2

    # Removing the inner one first splices it out of the chain.

    inner.remove()
    assert config.TIMEOUT == 31
    assert outer.active and not inner.active

    outer.remove()
    assert config.TIMEOUT == 30
    assert type(config) is types.ModuleType


def test_remove_in_either_order(config: Any) -> None:
    first = binding(config, "TIMEOUT").apply()
    second = binding(config, "TIMEOUT").apply()

    first.remove()
    assert second.active and not first.active
    assert config.TIMEOUT == 30

    second.remove()
    assert type(config) is types.ModuleType


# ---------------------------------------------------------------------------
# missing names and module __getattr__
# ---------------------------------------------------------------------------


def test_missing_name_needs_missing_ok(config: Any) -> None:
    # Detection resolves the name and fails there; an explicit mode=
    # defers the check to apply, where the missing_ok hint is given.

    with pytest.raises(Exception, match="NOPE"):
        binding(config, "NOPE")

    with pytest.raises(AttributeError, match="missing_ok"):
        binding(config, "NOPE", mode="attribute").apply()

    assert type(config) is types.ModuleType


def test_missing_ok_sees_later_assignment(config: Any) -> None:
    later = binding(config, "LATER", missing_ok=True)

    with later, wrapture.timeline() as tape:
        with pytest.raises(AttributeError):
            _ = config.LATER
        config.LATER = 1
        assert config.LATER == 1
        del config.LATER

    assert [event.kind for event in tape.for_binding(later)] == [
        "get",
        "set",
        "get",
        "delete",
    ]
    assert tape.for_binding(later)[0].exception is not None
    assert type(config) is types.ModuleType


def test_missing_ok_defers_to_module_getattr(config: Any) -> None:
    vars(config)["__getattr__"] = lambda name: f"virtual {name}"
    ghost = binding(config, "GHOST", missing_ok=True)
    ghost.on_get.then(after=1).returns("stubbed")

    with ghost:
        assert config.GHOST == "virtual GHOST"
        assert config.GHOST == "stubbed"

    assert config.GHOST == "virtual GHOST"


# ---------------------------------------------------------------------------
# dotted paths and submodules
# ---------------------------------------------------------------------------


def test_dotted_path_to_submodule_attribute(config: Any) -> None:
    sub = types.ModuleType("wrapture_modattr_config.sub")
    vars(sub)["LEVEL"] = "info"
    vars(config)["sub"] = sub

    level = binding(config, "sub.LEVEL")
    level.on_get.returns("debug")

    assert level.label == "wrapture_modattr_config.sub.LEVEL"

    with level:
        assert config.sub.LEVEL == "debug"
        assert type(config) is types.ModuleType

    assert type(sub) is types.ModuleType
    assert sub.LEVEL == "info"


# ---------------------------------------------------------------------------
# interaction with value bindings and class swaps
# ---------------------------------------------------------------------------


def test_value_binding_on_top_is_seen_as_set(config: Any) -> None:
    watched = binding(config, "TIMEOUT")
    held = binding(config, attr="TIMEOUT").overrides(99)

    with watched, wrapture.timeline() as tape:
        with held:
            assert config.TIMEOUT == 99
        assert config.TIMEOUT == 30

    kinds = [event.kind for event in tape.for_binding(watched)]
    assert kinds == ["set", "get", "set", "get"]


def test_class_swapped_on_top_is_left_in_place(config: Any) -> None:
    timeout = binding(config, "TIMEOUT").apply()
    ours = type(config)

    class Layered(ours):  # type: ignore[misc, valid-type]
        pass

    _swap_class(config, Layered)
    assert config.TIMEOUT == 30

    timeout.remove()
    assert not timeout.active
    assert type(config) is Layered
    assert _module_class(config) is None
    assert "TIMEOUT" not in vars(ours)

    # A fresh binding layers again above the foreign class.

    again = binding(config, "TIMEOUT").apply()
    assert type(config) is not Layered
    assert issubclass(type(config), Layered)
    again.remove()
    assert type(config) is Layered

    _swap_class(config, types.ModuleType)


def test_existing_subclass_is_preserved_beneath(config: Any) -> None:
    class Lazy(types.ModuleType):
        def __getattr__(self, name: str) -> Any:
            return f"lazy {name}"

    _swap_class(config, Lazy)
    timeout = binding(config, "TIMEOUT")

    with timeout:
        assert issubclass(type(config), Lazy)
        assert config.TIMEOUT == 30
        assert config.UNKNOWN == "lazy UNKNOWN"

    assert type(config) is Lazy
    _swap_class(config, types.ModuleType)


def test_refused_when_class_cannot_be_assigned() -> None:
    class Stuck(types.ModuleType):
        @property
        def __class__(self) -> type:
            return type(self)

        @__class__.setter
        def __class__(self, value: type) -> None:
            raise TypeError("__class__ assignment is not allowed")

    stuck = Stuck("stuck")
    vars(stuck)["TIMEOUT"] = 1

    with pytest.raises(TypeError, match="attr='TIMEOUT'"):
        binding(stuck, "TIMEOUT").apply()

    assert type(stuck) is Stuck
