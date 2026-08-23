"""Tests for instrumentation packages: the Instrumentation base class
and its instance contract, the [[instrument]] config entry in every
name form, the checks between entries, version gating, the live
registry with its one trampoline per trigger, and the
instrumentation() context manager.

Registered instrumentation is exercised through real dist-info
directories written under tmp_path and put on sys.path, so entry point
discovery, the distribution identity and the version lookup all go
through importlib.metadata itself rather than a fake.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
import threading
import types
import warnings
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import wrapt

import wrapture
from wrapture import (
    Config,
    ConfigError,
    ConfigWarning,
    Instrumentation,
    InstrumentEntry,
    Setting,
    instrumentation,
    load_config,
)
from wrapture.instrumentations import _active, _trampolines

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules() -> Iterator[None]:
    # Every module a test injects or writes carries the cfgi prefix, so
    # the fixture can forget them afterwards; the live registry must
    # also be empty when a test ends, or it leaked an application.

    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("cfgi"):
            del sys.modules[name]

    assert _active() == {}, "a test left instrumentation live"


def _fake_module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _write_module(directory: Path, name: str, source: str) -> None:
    (directory / f"{name}.py").write_text(textwrap.dedent(source))


def _install(
    site: Path,
    *,
    distribution: str,
    version: str,
    entries: Mapping[str, str],
    summary: str = "",
    top_level: tuple[str, ...] = (),
    url: str | None = None,
) -> None:
    # A minimal installed distribution: METADATA, the entry points, and
    # optionally top_level.txt so packages_distributions() can map a
    # target import name back to it.

    info = site / f"{distribution.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)

    lines = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {version}",
    ]
    if summary:
        lines.append(f"Summary: {summary}")
    if url:
        lines.append(f"Home-page: {url}")
    (info / "METADATA").write_text("\n".join(lines) + "\n")

    points = "".join(f"{name} = {value}\n" for name, value in entries.items())
    (info / "entry_points.txt").write_text(f"[wrapture.instrumentation]\n{points}")

    if top_level:
        (info / "top_level.txt").write_text("\n".join(top_level) + "\n")


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


def _patched(cls: type) -> bool:
    # Whether the class's charge method carries a wrapt wrapper, read
    # from the class dict so the bound-method view does not intervene.

    return isinstance(vars(cls)["charge"], wrapt.FunctionWrapper)


class Shop(Instrumentation):
    """Observe the shop's gateway."""

    target = "cfgi_shop"
    modules = ("cfgi_shop",)
    removable = True
    settings = {
        "threshold": Setting(100, "charges at or below this are not recorded"),
        "label": Setting("gateway", "the label the binding carries"),
    }

    seen: list[tuple[str, str | None]] = []

    def apply(self, name: str, module: Any) -> None:
        Shop.seen.append((name, self.trigger))

        label = self.settings["label"]
        charge = wrapture.binding(module.Gateway, "charge", label=label).apply()

        self.on_remove(charge.remove)


class TwoTriggers(Instrumentation):
    target = "cfgi_two"
    modules = ("cfgi_two", "cfgi_two.sub")
    removable = True

    def apply(self, name: str, module: Any) -> None:
        undone: list[str] = module.undone

        def undo(name: str = name) -> None:
            undone.append(name)

        self.on_remove(undo)


# ---------------------------------------------------------------------------
# class data validation
# ---------------------------------------------------------------------------


def test_a_subclass_must_declare_a_target() -> None:
    with pytest.raises(ConfigError, match="target must name"):

        class Missing(Instrumentation):
            modules = ("x",)

            def apply(self, name: str, module: Any) -> None:
                pass


def test_a_target_is_one_top_level_name() -> None:
    with pytest.raises(ConfigError, match="no dots"):

        class Dotted(Instrumentation):
            target = "flask.app"

            def apply(self, name: str, module: Any) -> None:
                pass


def test_every_trigger_must_live_under_the_target() -> None:
    with pytest.raises(ConfigError, match="not under target"):

        class Astray(Instrumentation):
            target = "flask"
            modules = ("flask.app", "werkzeug.routing")

            def apply(self, name: str, module: Any) -> None:
                pass


def test_settings_must_be_declared_with_setting() -> None:
    with pytest.raises(ConfigError, match="wrapture.Setting"):

        class Bare(Instrumentation):
            target = "flask"
            settings = {"threshold": 100}

            def apply(self, name: str, module: Any) -> None:
                pass


def test_specifiers_are_checked_at_class_load() -> None:
    with pytest.raises(ConfigError, match="not a valid PEP 440 specifier"):

        class BadRange(Instrumentation):
            target = "flask"
            supports = "lots"

            def apply(self, name: str, module: Any) -> None:
                pass

    with pytest.raises(ConfigError, match="not a valid PEP 440 specifier"):

        class BadModuleRange(Instrumentation):
            target = "flask"
            modules = {"flask.app": "??"}

            def apply(self, name: str, module: Any) -> None:
                pass


def test_modules_and_requires_have_their_shapes_checked() -> None:
    with pytest.raises(ConfigError, match="modules must be"):

        class StringModules(Instrumentation):
            target = "flask"
            modules = "flask.app"  # type: ignore[assignment]

            def apply(self, name: str, module: Any) -> None:
                pass

    with pytest.raises(ConfigError, match="requires must be"):

        class StringRequires(Instrumentation):
            target = "flask"
            requires = "werkzeug"  # type: ignore[assignment]

            def apply(self, name: str, module: Any) -> None:
                pass


# ---------------------------------------------------------------------------
# construction and settings
# ---------------------------------------------------------------------------


def test_construction_resolves_settings_over_the_defaults() -> None:
    instance = Shop(threshold=400)

    assert instance.settings == {"threshold": 400, "label": "gateway"}
    assert dict(Shop.settings) == {
        "threshold": Shop.settings["threshold"],
        "label": Shop.settings["label"],
    }
    assert instance.name == "cfgi_shop"
    assert instance.description == "Observe the shop's gateway."
    assert instance.version == ""
    assert instance.distribution is None
    assert instance.applied == ()
    assert instance.pending == ()
    assert instance.trigger is None


def test_the_resolved_settings_are_read_only() -> None:
    instance = Shop()

    with pytest.raises(TypeError):
        instance.settings["threshold"] = 1


def test_an_unknown_setting_is_refused() -> None:
    with pytest.raises(ConfigError, match=r"unknown settings \['treshold'\]"):
        Shop(treshold=400)


@pytest.mark.parametrize(
    ("default", "value", "expected"),
    [
        (False, "no", "a boolean"),
        (False, 0, "a boolean"),
        (100, "100", "an integer"),
        (100, True, "an integer"),
        (1.5, "1.5", "a number"),
        ("x", 1, "a string"),
        ((), "a,b", "a list"),
        ({}, [], "a table"),
    ],
)
def test_a_value_of_the_wrong_outer_type_is_refused(
    default: Any, value: Any, expected: str
) -> None:
    class Typed(Instrumentation):
        target = "cfgi_typed"
        settings = {"knob": Setting(default, "a knob")}

        def apply(self, name: str, module: Any) -> None:
            pass

    with pytest.raises(ConfigError, match=f"expects {expected}"):
        Typed(knob=value)


def test_the_outer_type_check_has_its_tolerances() -> None:
    class Tolerant(Instrumentation):
        target = "cfgi_tolerant"
        settings = {
            "ratio": Setting(0.5, "a float taking an int"),
            "paths": Setting((), "a tuple taking a list"),
            "extra": Setting(None, "anything at all"),
            "table": Setting({"a": 1}, "a mapping"),
        }

        def apply(self, name: str, module: Any) -> None:
            pass

    instance = Tolerant(ratio=1, paths=["/x"], extra=object(), table={"b": [1, 2]})

    assert instance.settings["ratio"] == 1
    assert instance.settings["paths"] == ["/x"]
    assert instance.settings["table"] == {"b": [1, 2]}


def test_nothing_inside_a_collection_is_checked() -> None:
    # Depth is the hook's job: configure() is where a shaped setting is
    # validated, and a ConfigError there surfaces at apply time.

    class Shaped(Instrumentation):
        target = "cfgi_shaped"
        modules = ("cfgi_shaped",)
        removable = True
        settings = {"routes": Setting((), "each {path, methods}")}

        def configure(self) -> None:
            for route in self.settings["routes"]:
                if not isinstance(route, Mapping) or "path" not in route:
                    raise ConfigError(f"routes: each entry needs a path, got {route!r}")

        def apply(self, name: str, module: Any) -> None:
            pass

    Shaped(routes=[{"methods": ["GET"]}])

    _fake_module("cfgi_shaped")
    with pytest.raises(ConfigError, match="each entry needs a path"):
        Config(
            instrument=[InstrumentEntry(Shaped, settings={"routes": [{"x": 1}]})]
        ).apply()


def test_setting_description_must_be_a_string() -> None:
    with pytest.raises(TypeError, match="description must be a string"):
        Setting(1, 2)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# applying: triggers, removal, the instance contract
# ---------------------------------------------------------------------------


def test_apply_fires_at_once_for_an_imported_trigger_and_removes_on_revert() -> None:
    module = _fake_module("cfgi_shop", Gateway=Gateway)
    del Shop.seen[:]

    applied = Config(
        instrument=[InstrumentEntry(Shop, settings={"threshold": 400})]
    ).apply()
    try:
        (instance,) = applied.instrumentations
        assert isinstance(instance, Shop)
        assert Shop.seen == [("cfgi_shop", "cfgi_shop")]
        assert instance.applied == ("cfgi_shop",)
        assert instance.pending == ()
        assert instance.trigger is None
        assert instance.settings["threshold"] == 400
        assert _patched(module.Gateway)
    finally:
        applied.revert()

    assert not _patched(module.Gateway)
    assert _active() == {}


def test_apply_fires_when_the_trigger_is_imported_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_module(tmp_path, "cfgi_lazy_shop", "class Gateway:\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    class LazyShop(Instrumentation):
        target = "cfgi_lazy_shop"
        modules = ("cfgi_lazy_shop",)
        removable = True
        fired: list[str] = []

        def apply(self, name: str, module: Any) -> None:
            LazyShop.fired.append(module.__name__)

    applied = Config(instrument=[InstrumentEntry(LazyShop)]).apply()
    try:
        (instance,) = applied.instrumentations
        assert instance.pending == ("cfgi_lazy_shop",)
        assert LazyShop.fired == []

        importlib.import_module("cfgi_lazy_shop")

        assert LazyShop.fired == ["cfgi_lazy_shop"]
        assert instance.applied == ("cfgi_lazy_shop",)
        assert not instance.pending
    finally:
        applied.revert()


def test_undo_callbacks_run_per_trigger_most_recent_first() -> None:
    one = _fake_module("cfgi_two", undone=[])
    two = _fake_module("cfgi_two.sub", undone=one.undone)
    del two

    applied = Config(instrument=[InstrumentEntry(TwoTriggers)]).apply()
    (instance,) = applied.instrumentations
    assert instance.applied == ("cfgi_two", "cfgi_two.sub")

    applied.revert()

    # Triggers come down in reverse firing order, each running the
    # callbacks its own apply() registered.

    assert one.undone == ["cfgi_two.sub", "cfgi_two"]


def test_several_callbacks_for_one_trigger_run_most_recent_first() -> None:
    order: list[str] = []

    class Many(Instrumentation):
        target = "cfgi_many"
        modules = ("cfgi_many",)
        removable = True

        def apply(self, name: str, module: Any) -> None:
            self.on_remove(lambda: order.append("first"))
            self.on_remove(lambda: order.append("second"))
            self.on_remove(lambda: order.append("second"))

    _fake_module("cfgi_many")
    Config(instrument=[InstrumentEntry(Many)]).apply().revert()

    assert order == ["second", "second", "first"]


def test_a_callback_registered_from_configure_runs_at_whole_teardown() -> None:
    order: list[str] = []

    class Configured(Instrumentation):
        target = "cfgi_configured"
        modules = ("cfgi_configured",)
        removable = True

        def configure(self) -> None:
            assert self.trigger is None
            self.on_remove(lambda: order.append("whole"))

        def apply(self, name: str, module: Any) -> None:
            self.on_remove(lambda: order.append(name))

    _fake_module("cfgi_configured")
    Config(instrument=[InstrumentEntry(Configured)]).apply().revert()

    assert order == ["cfgi_configured", "whole"]


def test_a_raising_callback_warns_and_the_rest_still_run() -> None:
    order: list[str] = []

    def broken() -> None:
        raise RuntimeError("undo broke")

    class Fragile(Instrumentation):
        target = "cfgi_fragile"
        modules = ("cfgi_fragile",)
        removable = True

        def apply(self, name: str, module: Any) -> None:
            self.on_remove(lambda: order.append("kept"))
            self.on_remove(broken)

    _fake_module("cfgi_fragile")
    applied = Config(instrument=[InstrumentEntry(Fragile)]).apply()

    with pytest.warns(ConfigWarning, match="undo broke"):
        applied.revert()

    assert order == ["kept"]


def test_an_overridden_remove_is_the_teardown() -> None:
    calls: list[tuple[str, str]] = []

    class Central(Instrumentation):
        target = "cfgi_central"
        modules = ("cfgi_central",)
        removable = True

        def apply(self, name: str, module: Any) -> None:
            calls.append(("apply", name))

        def remove(self, name: str, module: Any) -> None:
            calls.append(("remove", module.__name__))

    _fake_module("cfgi_central")
    Config(instrument=[InstrumentEntry(Central)]).apply().revert()

    assert calls == [("apply", "cfgi_central"), ("remove", "cfgi_central")]


def test_on_remove_takes_only_callables() -> None:
    with pytest.raises(TypeError, match="takes a callable"):
        Shop().on_remove("nope")  # type: ignore[arg-type]


def test_an_apply_that_raises_during_config_apply_unwinds_everything() -> None:
    undone: list[str] = []

    class Broken(Instrumentation):
        target = "cfgi_broken"
        modules = ("cfgi_broken",)
        removable = True

        def apply(self, name: str, module: Any) -> None:
            self.on_remove(lambda: undone.append("partial"))
            raise RuntimeError("hook code broke")

    _fake_module("cfgi_broken")
    _fake_module("cfgi_shop", Gateway=Gateway)

    with pytest.raises(RuntimeError, match="hook code broke"):
        Config(instrument=[InstrumentEntry(Shop), InstrumentEntry(Broken)]).apply()

    # The partial work of the failed trigger was undone at once, the
    # other instrumentation came down with the record, and the registry
    # holds neither.

    assert undone == ["partial"]
    assert _active() == {}
    assert not _patched(sys.modules["cfgi_shop"].Gateway)


def test_an_apply_that_raises_from_an_import_warns_and_the_import_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_module(tmp_path, "cfgi_late_broken", "value = 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    class LateBroken(Instrumentation):
        target = "cfgi_late_broken"
        modules = ("cfgi_late_broken",)
        removable = True

        def apply(self, name: str, module: Any) -> None:
            raise RuntimeError("hook code broke late")

    applied = Config(instrument=[InstrumentEntry(LateBroken)]).apply()
    try:
        with pytest.warns(ConfigWarning, match="hook code broke late"):
            module = importlib.import_module("cfgi_late_broken")

        assert module.value == 2
        (instance,) = applied.instrumentations
        assert instance.applied == ()
    finally:
        applied.revert()


def test_a_class_without_apply_is_refused_at_config_build() -> None:
    class Inert(Instrumentation):
        target = "cfgi_inert"

    with pytest.raises(ConfigError, match="does not define apply"):
        Config(instrument=[InstrumentEntry(Inert)])


def test_the_base_apply_raises_not_implemented() -> None:
    class Inert(Instrumentation):
        target = "cfgi_inert"

    with pytest.raises(NotImplementedError):
        Inert().apply("cfgi_inert", None)


def test_report_lists_instrumentation_and_disabled_entries() -> None:
    _fake_module("cfgi_shop", Gateway=Gateway)

    applied = Config(
        instrument=[
            InstrumentEntry(Shop),
            InstrumentEntry(TwoTriggers, enabled=False),
        ]
    ).apply()
    try:
        text = applied.report()
    finally:
        applied.revert()

    assert "instrumentation:" in text
    assert (
        "  cfgi_shop [local] target cfgi_shop (no version); applied cfgi_shop;"
        " removable; 1 undo callbacks" in text
    )
    assert f"  {__name__}:TwoTriggers: disabled" in text


def test_revert_warns_about_an_instrumentation_declared_not_removable() -> None:
    undone: list[str] = []

    class OneWay(Instrumentation):
        target = "cfgi_oneway"
        modules = ("cfgi_oneway",)

        def apply(self, name: str, module: Any) -> None:
            self.on_remove(lambda: undone.append(name))

    _fake_module("cfgi_oneway")
    applied = Config(instrument=[InstrumentEntry(OneWay)]).apply()

    with pytest.warns(ConfigWarning, match="declares itself not removable"):
        applied.revert()

    # The claim governs the warning; the callbacks run regardless.

    assert undone == ["cfgi_oneway"]


# ---------------------------------------------------------------------------
# checks between entries and against the live registry
# ---------------------------------------------------------------------------


def test_two_enabled_entries_for_one_target_conflict_at_build() -> None:
    class OtherShop(Instrumentation):
        target = "cfgi_shop"
        modules = ("cfgi_shop.other",)

        def apply(self, name: str, module: Any) -> None:
            pass

    with pytest.raises(ConfigError, match="both instrument target 'cfgi_shop'"):
        Config(instrument=[InstrumentEntry(Shop), InstrumentEntry(OtherShop)])

    # Disabled, the second takes part in no check.

    Config(
        instrument=[InstrumentEntry(Shop), InstrumentEntry(OtherShop, enabled=False)]
    )


def test_the_target_check_is_also_the_trigger_check() -> None:
    # Every trigger lives under its target, so two classes can only
    # claim one trigger module by sharing a target, and the target
    # check refuses that; classes with distinct targets never collide.

    class Rival(Instrumentation):
        target = "cfgi_two"
        modules = ("cfgi_two.sub",)

        def apply(self, name: str, module: Any) -> None:
            pass

    class Decoy(Instrumentation):
        target = "cfgi_decoy"
        modules = ("cfgi_decoy",)

        def apply(self, name: str, module: Any) -> None:
            pass

    with pytest.raises(ConfigError, match="both instrument target"):
        Config(instrument=[InstrumentEntry(TwoTriggers), InstrumentEntry(Rival)])

    Config(instrument=[InstrumentEntry(TwoTriggers), InstrumentEntry(Decoy)])


def test_a_required_target_must_be_enabled_in_the_same_config() -> None:
    class NeedsShop(Instrumentation):
        target = "cfgi_needy"
        modules = ("cfgi_needy",)
        requires = ("cfgi_shop",)

        def apply(self, name: str, module: Any) -> None:
            pass

    with pytest.raises(ConfigError, match="requires an active instrumentation"):
        Config(instrument=[InstrumentEntry(NeedsShop)])

    with pytest.raises(ConfigError, match="requires an active instrumentation"):
        Config(
            instrument=[
                InstrumentEntry(NeedsShop),
                InstrumentEntry(Shop, enabled=False),
            ]
        )

    Config(instrument=[InstrumentEntry(NeedsShop), InstrumentEntry(Shop)])


def test_a_second_config_cannot_instrument_a_live_target() -> None:
    _fake_module("cfgi_shop", Gateway=Gateway)

    first = Config(instrument=[InstrumentEntry(Shop)]).apply()
    try:
        with pytest.raises(ConfigError, match="already has 'cfgi_shop' active"):
            Config(instrument=[InstrumentEntry(Shop)]).apply()
    finally:
        first.revert()

    # Once reverted, the target is free again.

    Config(instrument=[InstrumentEntry(Shop)]).apply().revert()


def test_a_failed_second_apply_leaves_the_first_intact() -> None:
    module = _fake_module("cfgi_shop", Gateway=Gateway)

    first = Config(instrument=[InstrumentEntry(Shop)]).apply()
    try:
        with pytest.raises(ConfigError):
            Config(instrument=[InstrumentEntry(Shop)]).apply()

        assert _patched(module.Gateway)
        assert list(_active()) == ["cfgi_shop"]
    finally:
        first.revert()


# ---------------------------------------------------------------------------
# name forms: references, registered names, qualified names
# ---------------------------------------------------------------------------


def test_a_reference_names_a_local_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_module(
        tmp_path,
        "cfgi_local_hooks",
        '''
        import wrapture

        class ShopInstrumentation(wrapture.Instrumentation):
            """Gateway charges over a threshold."""

            target = "cfgi_shop"
            modules = ("cfgi_shop",)
            removable = True
            settings = {"threshold": wrapture.Setting(100, "cutoff")}

            def apply(self, name, module):
                module.applied_with = self.settings["threshold"]
        ''',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = _fake_module("cfgi_shop", Gateway=Gateway)

    config = Config(
        instrument=[
            InstrumentEntry(
                "cfgi_local_hooks:ShopInstrumentation", settings={"threshold": 400}
            )
        ]
    )
    applied = config.apply()
    try:
        (instance,) = applied.instrumentations
        assert instance.name == "cfgi_shop"
        assert instance.description == "Gateway charges over a threshold."
        assert instance.version == ""
        assert instance.distribution is None
        assert module.applied_with == 400
    finally:
        applied.revert()


def test_a_bad_reference_fails_at_build() -> None:
    with pytest.raises(ConfigError, match="cannot import module 'cfgi_nowhere'"):
        Config(instrument=[InstrumentEntry("cfgi_nowhere:Thing")])

    with pytest.raises(ConfigError, match="has no attribute 'Nope'"):
        Config(instrument=[InstrumentEntry(f"{__name__}:Nope")])

    with pytest.raises(ConfigError, match="not an Instrumentation subclass"):
        Config(instrument=[InstrumentEntry(f"{__name__}:Gateway")])

    with pytest.raises(ConfigError, match="module:attr"):
        Config(instrument=[InstrumentEntry("cfgi_nowhere:")])


def test_an_unknown_registered_name_fails_at_build() -> None:
    with pytest.raises(ConfigError, match="no instrumentation named 'cfgi_absent'"):
        Config(instrument=[InstrumentEntry("cfgi_absent")])


def test_a_registered_name_resolves_through_the_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    _write_module(
        site,
        "cfgi_pkg_flaskish",
        """
        import wrapture

        class FlaskishInstrumentation(wrapture.Instrumentation):
            target = "cfgi_flaskish"
            modules = ("cfgi_flaskish",)
            removable = True
            settings = {"capture_headers": wrapture.Setting(False, "record headers")}

            def apply(self, name, module):
                module.instrumented = self.settings["capture_headers"]
        """,
    )
    _install(
        site,
        distribution="wrapture-instrumentation-flaskish",
        version="1.2.0",
        entries={"flaskish": "cfgi_pkg_flaskish:FlaskishInstrumentation"},
        summary="Request tracing for Flaskish applications",
    )
    monkeypatch.syspath_prepend(str(site))
    module = _fake_module("cfgi_flaskish")

    applied = Config(
        instrument=[InstrumentEntry("flaskish", settings={"capture_headers": True})]
    ).apply()
    try:
        (instance,) = applied.instrumentations
        assert instance.name == "flaskish"
        assert instance.description == "Request tracing for Flaskish applications"
        assert instance.version == "1.2.0"
        assert instance.distribution == "wrapture-instrumentation-flaskish"
        assert module.instrumented is True
        assert "flaskish [wrapture-instrumentation-flaskish 1.2.0]" in applied.report()
    finally:
        applied.revert()

    # The qualified spelling reaches the same class, normalised.

    for spelling in (
        "flaskish@wrapture-instrumentation-flaskish",
        "flaskish@Wrapture_Instrumentation.Flaskish",
    ):
        config = Config(instrument=[InstrumentEntry(spelling)])
        assert config._instrument_planned[0].resolved.cls.__name__ == (
            "FlaskishInstrumentation"
        )

    with pytest.raises(ConfigError, match="is registered by distribution 'other'"):
        Config(instrument=[InstrumentEntry("flaskish@other")])


def test_a_name_two_distributions_register_needs_qualifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    for suffix in ("a", "b"):
        _write_module(
            site,
            f"cfgi_dup_{suffix}",
            f"""
            import wrapture

            class Requestsish(wrapture.Instrumentation):
                target = "cfgi_requestsish"
                modules = ("cfgi_requestsish",)
                removable = True

                def apply(self, name, module):
                    module.provider = {suffix!r}
            """,
        )
        _install(
            site,
            distribution=f"wrapture-instrumentation-{suffix}",
            version="0.1",
            entries={"requestsish": f"cfgi_dup_{suffix}:Requestsish"},
        )
    monkeypatch.syspath_prepend(str(site))
    module = _fake_module("cfgi_requestsish")

    with pytest.raises(ConfigError, match="more than one installed") as info:
        Config(instrument=[InstrumentEntry("requestsish")])

    assert "requestsish@wrapture-instrumentation-a" in str(info.value)
    assert "requestsish@wrapture-instrumentation-b" in str(info.value)

    applied = Config(
        instrument=[InstrumentEntry("requestsish@wrapture-instrumentation-b")]
    ).apply()
    try:
        assert module.provider == "b"
        (instance,) = applied.instrumentations
        assert instance.distribution == "wrapture-instrumentation-b"
    finally:
        applied.revert()


def test_loading_a_class_that_imports_its_own_target_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_module(tmp_path, "cfgi_eager_target", "MARK = 1\n")
    _write_module(
        tmp_path,
        "cfgi_eager_hooks",
        """
        import wrapture
        import cfgi_eager_target

        class Eager(wrapture.Instrumentation):
            target = "cfgi_eager_target"
            modules = ("cfgi_eager_target",)
            removable = True

            def apply(self, name, module):
                pass
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.warns(ConfigWarning, match="should import only wrapture") as record:
        Config(instrument=[InstrumentEntry("cfgi_eager_hooks:Eager")])

    assert "cfgi_eager_target" in str(record[0].message)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def _versioned_target(
    site: Path, monkeypatch: pytest.MonkeyPatch, *, version: str
) -> None:
    # A distribution standing behind the cfgi_versioned import name,
    # so target_version resolves through packages_distributions().

    _install(
        site,
        distribution="cfgi-versioned-dist",
        version=version,
        entries={},
        top_level=("cfgi_versioned",),
    )
    monkeypatch.syspath_prepend(str(site))


def test_target_version_comes_from_the_distribution_behind_the_import_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _versioned_target(tmp_path, monkeypatch, version="2.5.1")

    class Versioned(Instrumentation):
        target = "cfgi_versioned"

        def apply(self, name: str, module: Any) -> None:
            pass

    assert Versioned().target_version == "2.5.1"
    assert Shop().target_version is None


def test_supports_outside_the_installed_version_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _versioned_target(tmp_path, monkeypatch, version="1.9")
    _fake_module("cfgi_versioned")

    class Needs2(Instrumentation):
        target = "cfgi_versioned"
        supports = ">=2.0,<4"
        modules = ("cfgi_versioned",)
        removable = True
        fired = False

        def apply(self, name: str, module: Any) -> None:
            Needs2.fired = True

    with pytest.warns(ConfigWarning, match="outside supports '>=2.0,<4'"):
        applied = Config(instrument=[InstrumentEntry(Needs2)]).apply()
    try:
        (instance,) = applied.instrumentations
        assert not Needs2.fired
        assert instance.applied == ()
        assert instance.pending == ()
    finally:
        applied.revert()


def test_a_per_module_specifier_skips_only_that_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _versioned_target(tmp_path, monkeypatch, version="2.2")
    _fake_module("cfgi_versioned")
    _fake_module("cfgi_versioned.sansio")

    class Split(Instrumentation):
        target = "cfgi_versioned"
        supports = ">=2.0"
        modules = {"cfgi_versioned": None, "cfgi_versioned.sansio": ">=2.3"}
        removable = True
        fired: list[str] = []

        def apply(self, name: str, module: Any) -> None:
            Split.fired.append(name)

    with pytest.warns(ConfigWarning, match="skips 1 of 2 trigger modules"):
        applied = Config(instrument=[InstrumentEntry(Split)]).apply()
    try:
        assert Split.fired == ["cfgi_versioned"]
    finally:
        applied.revert()


def test_an_unknown_version_skips_only_constrained_triggers() -> None:
    _fake_module("cfgi_unknown")
    _fake_module("cfgi_unknown.new")

    class Unversioned(Instrumentation):
        target = "cfgi_unknown"
        modules = {"cfgi_unknown": None, "cfgi_unknown.new": ">=3"}
        removable = True
        fired: list[str] = []

        def apply(self, name: str, module: Any) -> None:
            Unversioned.fired.append(name)

    with pytest.warns(ConfigWarning, match="target version unknown"):
        applied = Config(instrument=[InstrumentEntry(Unversioned)]).apply()
    try:
        assert Unversioned.fired == ["cfgi_unknown"]
    finally:
        applied.revert()

    class Claims(Instrumentation):
        target = "cfgi_unknown"
        supports = ">=1"
        modules = ("cfgi_unknown",)
        removable = True
        fired: list[str] = []

        def apply(self, name: str, module: Any) -> None:
            Claims.fired.append(name)

    with pytest.warns(ConfigWarning, match="cannot be checked"):
        applied = Config(instrument=[InstrumentEntry(Claims)]).apply()
    try:
        assert Claims.fired == []
    finally:
        applied.revert()


# ---------------------------------------------------------------------------
# the TOML loader
# ---------------------------------------------------------------------------


def test_the_loader_reads_instrument_entries(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[instrument]]
            name = "{__name__}:Shop"
            threshold = 400

            [[instrument]]
            name = "{__name__}:TwoTriggers"
            enabled = false
            """
        )
    )

    config = load_config(source)

    first, second = config.instrument
    assert first.name == f"{__name__}:Shop"
    assert first.enabled is True
    assert first.settings == {"threshold": 400}
    assert second.enabled is False
    assert second.settings == {}


def test_the_loader_validates_settings_against_the_declaration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(f'[[instrument]]\nname = "{__name__}:Shop"\ntreshold = 400\n')

    with pytest.raises(ConfigError, match=r"unknown settings \['treshold'\]"):
        load_config(source)

    source.write_text(f'[[instrument]]\nname = "{__name__}:Shop"\nthreshold = "400"\n')

    with pytest.raises(ConfigError, match="expects an integer"):
        load_config(source)

    # A disabled entry is validated all the same.

    source.write_text(
        f'[[instrument]]\nname = "{__name__}:Shop"\nenabled = false\ntreshold = 1\n'
    )

    with pytest.raises(ConfigError, match="unknown settings"):
        load_config(source)


def test_the_loader_rejects_malformed_instrument_tables(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"

    source.write_text("[[instrument]]\nthreshold = 400\n")
    with pytest.raises(ConfigError, match="name key is required"):
        load_config(source)

    source.write_text(f'[[instrument]]\nname = "{__name__}:Shop"\nenabled = "no"\n')
    with pytest.raises(ConfigError, match="enabled must be true or false"):
        load_config(source)

    source.write_text('[instrument]\nname = "x"\n')
    with pytest.raises(ConfigError, match=r"write each as an \[\[instrument\]\] entry"):
        load_config(source)

    source.write_text('[[setup]]\nmodule = "x"\n')
    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(source)


def test_instrument_entry_validation() -> None:
    with pytest.raises(ConfigError, match="non-empty string"):
        InstrumentEntry("")

    with pytest.raises(ConfigError, match="Instrumentation subclass"):
        InstrumentEntry(Gateway)  # type: ignore[arg-type]

    with pytest.raises(ConfigError, match="settings must be a mapping"):
        InstrumentEntry("x", settings="loud")  # type: ignore[arg-type]

    with pytest.raises(ConfigError, match="must be InstrumentEntry instances"):
        Config(instrument=["flask"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# the context manager
# ---------------------------------------------------------------------------


def test_instrumentation_scopes_an_instrumentation_to_a_block() -> None:
    module = _fake_module("cfgi_shop", Gateway=Gateway)

    with instrumentation(Shop, threshold=9) as record:
        (instance,) = record.instrumentations
        assert instance.settings["threshold"] == 9
        assert _patched(module.Gateway)

    assert not _patched(module.Gateway)
    assert _active() == {}


def test_instrumentation_takes_names_classes_and_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_module("cfgi_shop", Gateway=Gateway)
    _fake_module("cfgi_two", undone=[])
    _fake_module("cfgi_two.sub", undone=[])

    with instrumentation((f"{__name__}:Shop", {"threshold": 3}), TwoTriggers) as record:
        first, second = record.instrumentations
        assert first.settings["threshold"] == 3
        assert second.applied == ("cfgi_two", "cfgi_two.sub")


def test_keyword_settings_need_exactly_one_item() -> None:
    with pytest.raises(ConfigError, match="exactly one item"):
        instrumentation(Shop, TwoTriggers, threshold=1)

    with pytest.raises(ConfigError, match="at least one"):
        instrumentation()

    with pytest.raises(ConfigError, match=r"\(name, settings\)"):
        instrumentation((Shop, {}, 1))


def test_instrumentation_refuses_an_unremovable_class_unless_allowed() -> None:
    class OneWay(Instrumentation):
        target = "cfgi_oneway"
        modules = ("cfgi_oneway",)

        def apply(self, name: str, module: Any) -> None:
            pass

    _fake_module("cfgi_oneway")

    with pytest.raises(ConfigError, match="declares itself not removable"):
        instrumentation(OneWay)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConfigWarning)
        with instrumentation(OneWay, allow_unremovable=True) as record:
            assert record.instrumentations[0].applied == ("cfgi_oneway",)


def test_instrumentation_refuses_a_target_another_record_holds() -> None:
    _fake_module("cfgi_shop", Gateway=Gateway)

    applied = Config(instrument=[InstrumentEntry(Shop)]).apply()
    try:
        with pytest.raises(ConfigError, match="already has"):
            with instrumentation(Shop):
                pass
    finally:
        applied.revert()


def test_instrumentation_pairs_with_timeline() -> None:
    module = _fake_module("cfgi_shop", Gateway=Gateway)

    with instrumentation(Shop, label="shop.charge"), wrapture.timeline() as tape:
        module.Gateway().charge(500)

    (event,) = tape.all
    assert event.label == "shop.charge"
    assert event.result == "ch_500"


def test_an_instrumented_scope_cannot_be_entered_twice() -> None:
    _fake_module("cfgi_shop", Gateway=Gateway)

    scope = instrumentation(Shop)
    with scope:
        with pytest.raises(RuntimeError, match="already active"):
            scope.__enter__()


def test_repeated_scopes_do_not_accumulate_hooks_for_an_unimported_trigger() -> None:
    class Never(Instrumentation):
        target = "cfgi_never_imported"
        modules = ("cfgi_never_imported",)
        removable = True

        def apply(self, name: str, module: Any) -> None:
            pass

    assert "cfgi_never_imported" not in sys.modules

    for _ in range(5):
        with instrumentation(Never):
            pass

    # One trampoline with wrapt, holding no claims once every scope has
    # exited, however many times the suite enters and leaves.

    importer = importlib.import_module("wrapt.importer")
    hooks = importer._post_import_hooks.get("cfgi_never_imported", [])
    assert len(hooks) == 1
    assert _trampolines["cfgi_never_imported"].claims == []


def test_a_released_claim_does_not_fire_when_the_module_finally_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_module(tmp_path, "cfgi_arrives_late", "MARK = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    class Late(Instrumentation):
        target = "cfgi_arrives_late"
        modules = ("cfgi_arrives_late",)
        removable = True
        fired = False

        def apply(self, name: str, module: Any) -> None:
            Late.fired = True

    with instrumentation(Late):
        pass

    importlib.import_module("cfgi_arrives_late")
    assert not Late.fired

    # And a fresh scope after the import fires at once.

    with instrumentation(Late):
        assert Late.fired


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------


def test_overlapping_applies_on_two_threads_keep_their_own_trigger() -> None:
    # Two triggers firing concurrently on two threads, as two modules
    # imported at once would: each apply() sees its own trigger, and
    # the callbacks each registers are filed under the right module,
    # so a per-trigger remove() runs only its own. The firing is driven
    # directly rather than through the import system, whose overlap of
    # sibling imports varies between Python versions.

    barrier = threading.Barrier(2)
    seen: dict[str, str | None] = {}
    undone: list[str] = []

    class Parallel(Instrumentation):
        target = "cfgi_par"
        modules = ("cfgi_par.a", "cfgi_par.b")
        removable = True

        def apply(self, name: str, module: Any) -> None:
            barrier.wait(timeout=5)
            seen[name] = self.trigger
            self.on_remove(lambda: undone.append(name))

    instance = Parallel()
    modules = {name: _fake_module(name) for name in ("cfgi_par.a", "cfgi_par.b")}

    threads = [
        threading.Thread(target=instance._fire, args=(name, module))
        for name, module in modules.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert seen == {"cfgi_par.a": "cfgi_par.a", "cfgi_par.b": "cfgi_par.b"}
    assert sorted(instance.applied) == ["cfgi_par.a", "cfgi_par.b"]
    assert instance.trigger is None

    instance.remove("cfgi_par.b", modules["cfgi_par.b"])
    assert undone == ["cfgi_par.b"]

    instance._teardown()
    assert undone == ["cfgi_par.b", "cfgi_par.a"]
