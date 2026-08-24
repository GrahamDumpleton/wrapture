"""Tests for mapping bindings: substituting the content of one mapping
object in place, so every holder of it sees the change."""

import os
import sys
import types
from collections.abc import Iterator
from types import MappingProxyType
from typing import Any

import pytest

import wrapture
from wrapture import WrongModeError, binding, bindings

config: Any = types.ModuleType("wrapture_mapping_config")
config.SETTINGS = {"currency": "USD", "tax_rate": 0.2}
config.DATABASE = {"primary": {"host": "db", "port": 5432}}
config.VERSION = 1.5
sys.modules["wrapture_mapping_config"] = config

# A holder of the same dict, as `from config import SETTINGS` produces.
SETTINGS = config.SETTINGS


@pytest.fixture(autouse=True)
def _content_restored() -> Iterator[None]:
    yield

    assert config.SETTINGS is SETTINGS
    assert list(SETTINGS.items()) == [("currency", "USD"), ("tax_rate", 0.2)]
    assert config.DATABASE == {"primary": {"host": "db", "port": 5432}}


# ---------------------------------------------------------------------------
# creation
# ---------------------------------------------------------------------------


def test_mode_mapping_on_a_location() -> None:
    settings = binding("wrapture_mapping_config", "SETTINGS", mode="mapping")

    assert settings.mode == "mapping"
    assert settings.label is None
    assert settings.path == "wrapture_mapping_config:SETTINGS"
    assert repr(settings) == (
        "<Binding 'wrapture_mapping_config:SETTINGS' mapping unapplied>"
    )


def test_every_location_form_names_the_same_mapping() -> None:
    forms = [
        binding("wrapture_mapping_config", "SETTINGS", mode="mapping"),
        binding("wrapture_mapping_config:SETTINGS", mode="mapping"),
        binding(config, "SETTINGS", mode="mapping"),
        binding(SETTINGS, mode="mapping"),
    ]

    for form in forms:
        with form.overrides({"currency": "EUR"}):
            assert SETTINGS == {"currency": "EUR"}
        assert SETTINGS == {"currency": "USD", "tax_rate": 0.2}

    assert forms[3].label is None
    assert forms[3].path == "builtins:dict"


def test_bare_object_paths() -> None:
    assert binding(os.environ, mode="mapping").path == "os:_Environ"
    assert binding(os, "environ", mode="mapping").label is None
    assert binding(os, "environ", mode="mapping").path == "os:environ"


def test_item_slot_reaches_a_nested_mapping() -> None:
    primary = binding(config, "DATABASE", item="primary", mode="mapping")

    assert primary.mode == "mapping"
    assert primary.label is None
    assert primary.path == "wrapture_mapping_config:DATABASE['primary']"

    with primary.updates({"port": 5433}):
        assert config.DATABASE["primary"] == {"host": "db", "port": 5433}

    assert config.DATABASE["primary"] == {"host": "db", "port": 5432}


def test_bare_object_without_the_mode_is_still_an_error() -> None:
    with pytest.raises(TypeError, match="names no attribute"):
        binding(SETTINGS)


def test_refusals() -> None:
    with pytest.raises(TypeError, match="mutable mapping.*got module"):
        binding(config, mode="mapping")

    with pytest.raises(TypeError, match="mutable mapping.*got mappingproxy"):
        binding(MappingProxyType({}), mode="mapping")

    with pytest.raises(TypeError, match="mutable mapping.*got float"):
        binding(config, "VERSION", mode="mapping")

    with pytest.raises(TypeError, match="name it positionally"):
        binding(config, attr="SETTINGS", mode="mapping")

    with pytest.raises(KeyError, match="no entry 'missing'"):
        binding(config, "DATABASE", item="missing", mode="mapping")

    with pytest.raises(ValueError, match="mapping binding records nothing"):
        binding(SETTINGS, mode="mapping", capture="snapshot")

    with pytest.raises(ValueError, match="mapping binding records nothing"):
        binding(config, "DATABASE", item="primary", mode="mapping", stack=1)


def test_verbs_take_a_mapping() -> None:
    settings = binding(SETTINGS, mode="mapping")

    with pytest.raises(TypeError, match="takes a mapping of entries, got list"):
        settings.overrides([("currency", "EUR")])

    with pytest.raises(TypeError, match="takes a mapping of entries, got NoneType"):
        settings.updates(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the verbs
# ---------------------------------------------------------------------------


def test_overrides_replaces_the_content_for_every_holder() -> None:
    settings = binding("wrapture_mapping_config", "SETTINGS", mode="mapping")

    with settings.overrides({"currency": "EUR"}):
        assert config.SETTINGS is SETTINGS
        assert SETTINGS == {"currency": "EUR"}
        assert "tax_rate" not in SETTINGS

    assert list(SETTINGS.items()) == [("currency", "USD"), ("tax_rate", 0.2)]


def test_overrides_empty_is_the_absent_content() -> None:
    with binding(SETTINGS, mode="mapping").overrides({}):
        assert SETTINGS == {}


def test_updates_merges_over_the_content() -> None:
    with binding(SETTINGS, mode="mapping").updates({"currency": "EUR", "locale": "de"}):
        assert SETTINGS == {"currency": "EUR", "tax_rate": 0.2, "locale": "de"}


def test_passes_through_is_the_initial_state_and_the_way_back() -> None:
    settings = binding(SETTINGS, mode="mapping")

    with settings:
        assert SETTINGS == {"currency": "USD", "tax_rate": 0.2}

        settings.overrides({"currency": "EUR"})
        assert SETTINGS == {"currency": "EUR"}

        settings.passes_through()
        assert list(SETTINGS.items()) == [("currency", "USD"), ("tax_rate", 0.2)]


def test_hides_is_refused_with_a_pointer() -> None:
    with pytest.raises(WrongModeError, match=r"use overrides\(\{\}\)"):
        binding(SETTINGS, mode="mapping").hides()


def test_updates_is_refused_on_a_value_binding() -> None:
    with pytest.raises(WrongModeError, match="only available on a mapping binding"):
        binding(config, attr="SETTINGS").updates({})

    with pytest.raises(WrongModeError, match="only available on a mapping binding"):
        binding(config, "SETTINGS", item="currency").updates({})


def test_verbs_on_other_modes_are_refused() -> None:
    class Gateway:
        def charge(self) -> None: ...

    with pytest.raises(WrongModeError, match="value or mapping binding"):
        binding(Gateway, "charge").overrides({})

    with pytest.raises(WrongModeError, match="only available on a mapping binding"):
        binding(Gateway, "charge").updates({})


def test_no_namespaces_or_events() -> None:
    settings = binding(SETTINGS, mode="mapping")

    with pytest.raises(WrongModeError, match=r"overrides\(\), updates\(\)"):
        _ = settings.on_call

    with pytest.raises(WrongModeError, match="mapping binding and records nothing"):
        _ = settings.events

    with pytest.raises(WrongModeError, match="cannot carry an expectation"):
        settings.expect_once()


# ---------------------------------------------------------------------------
# live reconfiguration
# ---------------------------------------------------------------------------


def test_reconfiguring_a_live_binding_in_every_direction() -> None:
    settings = binding(SETTINGS, mode="mapping")

    with settings:
        settings.updates({"locale": "de"})
        assert SETTINGS == {"currency": "USD", "tax_rate": 0.2, "locale": "de"}

        # updates to updates: the earlier key does not linger.
        settings.updates({"currency": "EUR"})
        assert SETTINGS == {"currency": "EUR", "tax_rate": 0.2}

        settings.overrides({"only": 1})
        assert SETTINGS == {"only": 1}

        settings.updates({"tax_rate": 0})
        assert SETTINGS == {"currency": "USD", "tax_rate": 0}

        settings.overrides({})
        assert SETTINGS == {}

        settings.passes_through()
        assert SETTINGS == {"currency": "USD", "tax_rate": 0.2}


def test_values_are_copied_at_the_verb_call() -> None:
    wanted = {"currency": "EUR"}
    settings = binding(SETTINGS, mode="mapping").overrides(wanted)
    wanted["currency"] = "GBP"

    with settings:
        assert SETTINGS == {"currency": "EUR"}


def test_the_fixture_shape() -> None:
    settings = binding("wrapture_mapping_config", "SETTINGS", mode="mapping")

    with settings:
        settings.updates({"currency": "EUR"})
        assert SETTINGS["currency"] == "EUR"

        settings.overrides({})
        assert SETTINGS == {}


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_suspend_and_resume() -> None:
    settings = binding(SETTINGS, mode="mapping").overrides({"currency": "EUR"})

    with settings:
        settings.suspend()
        assert SETTINGS == {"currency": "USD", "tax_rate": 0.2}
        assert settings.applied and settings.suspended and settings.active

        settings.resume()
        assert SETTINGS == {"currency": "EUR"}
        assert settings.active


def test_active_reports_displacement() -> None:
    settings = binding(SETTINGS, mode="mapping").overrides({"currency": "EUR"})

    with settings:
        assert settings.active
        SETTINGS["currency"] = "GBP"
        assert not settings.active
        assert repr(settings).endswith("mapping displaced>")

    merged = binding(SETTINGS, mode="mapping").updates({"currency": "EUR"})

    with merged:
        SETTINGS["extra"] = 1  # other keys are not the binding's
        assert merged.active
        del SETTINGS["currency"]
        assert not merged.active


def test_remove_restores_exact_content_and_order() -> None:
    ordered = {"b": 2, "a": 1, "c": 3}
    content = binding(ordered, mode="mapping")

    with content.overrides({"z": 26}):
        ordered["y"] = 25

    assert list(ordered.items()) == [("b", 2), ("a", 1), ("c", 3)]


def test_apply_is_atomic_on_a_mapping_that_refuses_a_value() -> None:
    before = dict(os.environ)
    env = binding(os, "environ", mode="mapping").overrides({"WRAPTURE_BAD": 1})

    with pytest.raises(TypeError):
        env.apply()

    assert dict(os.environ) == before
    assert not env.applied


def test_a_failed_reconfiguration_keeps_the_previous_state() -> None:
    env = binding(os, "environ", mode="mapping")

    with env.updates({"WRAPTURE_OK": "1"}):
        with pytest.raises(TypeError):
            env.updates({"WRAPTURE_BAD": 1})

        # The earlier configuration is still what it holds, and what
        # is written.

        assert os.environ["WRAPTURE_OK"] == "1"
        assert "WRAPTURE_BAD" not in os.environ
        assert env.active

    assert "WRAPTURE_OK" not in os.environ


def test_environment_emptied_and_restored() -> None:
    before = dict(os.environ)
    assert before

    with binding(os, "environ", mode="mapping").overrides({"ONLY": "this"}):
        assert dict(os.environ) == {"ONLY": "this"}
        assert os.getenv("PATH") is None

    assert dict(os.environ) == before


def test_group_with_an_item_binding_on_the_same_dict() -> None:
    group = bindings(
        content=binding(SETTINGS, mode="mapping").overrides({"currency": "EUR"}),
        rate=binding(SETTINGS, item="tax_rate").overrides(0),
    )

    with group:
        assert SETTINGS == {"currency": "EUR", "tax_rate": 0}

    assert list(SETTINGS.items()) == [("currency", "USD"), ("tax_rate", 0.2)]


def test_two_mapping_bindings_unwind_in_order() -> None:
    outer = binding(SETTINGS, mode="mapping").updates({"currency": "EUR"})
    inner = binding(SETTINGS, mode="mapping").updates({"tax_rate": 0})

    with outer:
        with inner:
            assert SETTINGS == {"currency": "EUR", "tax_rate": 0}
        assert SETTINGS == {"currency": "EUR", "tax_rate": 0.2}


def test_the_leak_sweep_sees_an_applied_mapping_binding() -> None:
    from wrapture.bindings import _applied_bindings

    settings = binding(SETTINGS, mode="mapping").apply()
    try:
        assert settings in _applied_bindings
    finally:
        settings.remove()

    assert settings not in _applied_bindings


def test_nested_values_are_the_same_objects() -> None:
    # Content is restored, values are not copied: a mutated nested
    # object stays mutated, as with patch.dict.

    nested = {"inner": {"n": 1}}

    with binding(nested, mode="mapping").overrides({"other": 2}):
        pass

    with binding(nested, mode="mapping"):
        nested["inner"]["n"] = 2

    assert nested == {"inner": {"n": 2}}


def test_wrapture_exports_nothing_new() -> None:
    assert not hasattr(wrapture, "updates")
