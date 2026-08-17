"""Tests for the config layer: the programmatic Config primitive and
the TOML file loader.

A Config bundles observe entries, a sink, setup callbacks, and the
capture and sampling settings; apply() installs the lot and unwinds
itself on failure. The TOML loader is a thin schema over the same
primitive, with loud failures for anything it does not understand.
"""

import importlib
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from wrapture import (
    AppliedConfig,
    Config,
    ConfigError,
    ConfigWarning,
    Event,
    JSONLines,
    ObserveEntry,
    Sample,
    SetupEntry,
    Sink,
    binding,
    find_config,
    load_config,
    remove_sink,
)


class Collector(Sink):
    def __init__(self) -> None:
        self.entered: list[Event] = []

    def on_enter(self, event: Event) -> None:
        self.entered.append(event)


class OrderService:
    limit = 10

    def place(self) -> str:
        return "placed"

    def cancel(self) -> str:
        return "cancelled"

    def _audit(self) -> str:
        return "audited"

    @staticmethod
    def validate(order: Any) -> Any:
        return order

    @classmethod
    def build(cls) -> "OrderService":
        return cls()

    @property
    def total(self) -> int:
        return 1

    class Receipt:
        pass


def parse_widget(text: str) -> str:
    return f"widget:{text}"


def _unwind(applied: AppliedConfig) -> None:
    for bound in reversed(applied.bindings):
        bound.remove()

    if applied.sink is not None:
        remove_sink(applied.sink)


# ---------------------------------------------------------------------------
# the programmatic primitive
# ---------------------------------------------------------------------------


def test_apply_installs_bindings_and_registers_the_sink() -> None:
    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target=f"{__name__}:OrderService", name="place")],
        sink=collector,
    )

    applied = config.apply()
    try:
        OrderService().place()
    finally:
        _unwind(applied)

    assert [event.path for event in collector.entered] == [
        f"{__name__}:OrderService.place"
    ]

    # Unwinding took the binding off again: nothing further records.

    OrderService().place()
    assert len(collector.entered) == 1


def test_a_named_member_must_exist() -> None:
    config = Config(
        observe=[ObserveEntry(target=f"{__name__}:OrderService", name="dispatch")]
    )

    with pytest.raises(ConfigError, match="no member named 'dispatch'"):
        config.apply()


def test_name_binds_a_property() -> None:
    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target=f"{__name__}:OrderService", name="total")],
        sink=collector,
    )

    applied = config.apply()
    try:
        _ = OrderService().total
    finally:
        _unwind(applied)

    (event,) = collector.entered
    assert event.kind == "get"


def test_an_unimportable_target_fails_loudly() -> None:
    config = Config(
        observe=[ObserveEntry(target="no_such_module_anywhere:Thing", name="run")]
    )

    with pytest.raises(ConfigError, match="cannot import module"):
        config.apply()


def test_match_selects_the_targets_own_routines_only() -> None:
    # The pattern sees one level of one named container: routines from
    # the class's own dict, including static and class methods, but
    # never the property, the nested class, or plain data; exclude
    # subtracts from the match.

    config = Config(
        observe=[
            ObserveEntry(target=f"{__name__}:OrderService", match="*", exclude="_*")
        ]
    )

    applied = config.apply()
    try:
        selected = {bound.name for bound in applied.bindings}
    finally:
        _unwind(applied)

    assert selected == {
        "OrderService.place",
        "OrderService.cancel",
        "OrderService.validate",
        "OrderService.build",
    }


def test_match_on_a_module_skips_imported_functions_and_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A module pattern selects only the functions the module itself
    # defines: functions imported from elsewhere and classes (whose
    # instantiation must never be bulk-wrapped) are skipped.

    (tmp_path / "cfgtest_widgets.py").write_text(
        textwrap.dedent(
            """
            from os.path import join

            def parse(text):
                return text

            def render(text):
                return text

            class Widget:
                pass
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    config = Config(observe=[ObserveEntry(target="cfgtest_widgets", match="*")])

    applied = config.apply()
    try:
        selected = {bound.name for bound in applied.bindings}
    finally:
        _unwind(applied)

    assert selected == {"parse", "render"}


def test_match_skips_already_wrapped_members() -> None:
    with binding(OrderService, "place"):
        config = Config(
            observe=[ObserveEntry(target=f"{__name__}:OrderService", match="place")]
        )

        with pytest.warns(ConfigWarning):
            applied = config.apply()

        try:
            assert applied.bindings == ()
        finally:
            _unwind(applied)


def test_a_match_selecting_nothing_warns() -> None:
    config = Config(
        observe=[ObserveEntry(target=f"{__name__}:OrderService", match="handle_*")]
    )

    with pytest.warns(ConfigWarning, match="selected no members"):
        applied = config.apply()

    _unwind(applied)


def test_the_capture_override_applies_to_every_binding() -> None:
    # The config's capture level rides on each binding, where it beats
    # what the sink declares: "none" skips argument capture entirely
    # even though the collector would ask for references.

    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target=__name__, name="parse_widget")],
        sink=collector,
        capture="none",
    )

    applied = config.apply()
    try:
        parse_widget("gear")
    finally:
        _unwind(applied)

    (event,) = collector.entered
    assert event.arguments is None


def test_sample_wraps_the_registered_sink() -> None:
    # Sampling wraps the sink at registration: rate 0.0 keeps no call
    # trees, so the inner sink hears nothing at all.

    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target=__name__, name="parse_widget")],
        sink=collector,
        sample=0.0,
    )

    applied = config.apply()
    try:
        assert isinstance(applied.sink, Sample)
        parse_widget("gear")
    finally:
        _unwind(applied)

    assert collector.entered == []


def test_sample_requires_a_sink() -> None:
    with pytest.raises(ConfigError, match="sample requires a sink"):
        Config(sample=0.5)


def test_a_failed_apply_leaves_nothing_behind() -> None:
    # The second entry fails, so the first entry's binding and the
    # sink must both be gone again when the error propagates.

    original = vars(OrderService)["place"]
    collector = Collector()

    config = Config(
        observe=[
            ObserveEntry(target=f"{__name__}:OrderService", name="place"),
            ObserveEntry(target=f"{__name__}:OrderService", name="dispatch"),
        ],
        sink=collector,
    )

    with pytest.raises(ConfigError, match="no member named 'dispatch'"):
        config.apply()

    assert vars(OrderService)["place"] is original

    OrderService().place()
    assert collector.entered == []


# ---------------------------------------------------------------------------
# entry validation
# ---------------------------------------------------------------------------


def test_name_and_match_are_mutually_exclusive() -> None:
    with pytest.raises(ConfigError, match="mutually exclusive"):
        ObserveEntry(target="mod:Thing", name="run", match="r*")


def test_one_of_name_or_match_is_required() -> None:
    with pytest.raises(ConfigError, match="one of name or match is required"):
        ObserveEntry(target="mod:Thing")


def test_exclude_requires_match() -> None:
    with pytest.raises(ConfigError, match="exclude only applies to match"):
        ObserveEntry(target="mod:Thing", name="run", exclude="_*")


def test_a_target_has_at_most_one_colon() -> None:
    with pytest.raises(ConfigError, match="single colon"):
        ObserveEntry(target="mod:Thing:run", name="go")


def test_a_setup_call_must_be_a_module_attr_reference() -> None:
    with pytest.raises(ConfigError, match="module:attr"):
        SetupEntry(module="mod", call="just_a_name")


# ---------------------------------------------------------------------------
# setup callbacks
# ---------------------------------------------------------------------------

_setup_calls: list[Any] = []


def _note_setup(module: Any) -> None:
    _setup_calls.append(module)


def test_a_setup_callback_fires_immediately_for_an_imported_module() -> None:
    del _setup_calls[:]

    config = Config(setup=[SetupEntry(module=__name__, call=f"{__name__}:_note_setup")])
    config.apply()

    assert _setup_calls == [sys.modules[__name__]]


def test_a_setup_callback_fires_when_the_module_is_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The trigger module does not exist yet when the config applies;
    # the callback runs only once it is imported, receiving the live
    # module, with the callback reference resolved at that moment.

    (tmp_path / "cfgtest_lazy_target.py").write_text("MARK = 'target'\n")
    (tmp_path / "cfgtest_lazy_hooks.py").write_text(
        "fired = []\n\ndef instrument(module):\n    fired.append(module.MARK)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    config = Config(
        setup=[
            SetupEntry(
                module="cfgtest_lazy_target",
                call="cfgtest_lazy_hooks:instrument",
            )
        ]
    )
    config.apply()

    assert "cfgtest_lazy_target" not in sys.modules

    importlib.import_module("cfgtest_lazy_target")

    hooks = sys.modules["cfgtest_lazy_hooks"]
    assert hooks.fired == ["target"]


def test_an_unresolvable_setup_callback_fails_at_fire_time() -> None:
    # The trigger module is already imported, so registration fires
    # the hook on the spot and the bad reference surfaces from apply.

    config = Config(
        setup=[SetupEntry(module=__name__, call=f"{__name__}:no_such_callback")]
    )

    with pytest.raises(ConfigError, match="no attribute 'no_such_callback'"):
        config.apply()


# ---------------------------------------------------------------------------
# the TOML loader
# ---------------------------------------------------------------------------


def test_load_config_reads_the_full_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pythonpath entries anchor to the config file's directory and are
    # prepended before references resolve, so the sink factory can
    # live in an uninstalled module right next to the config file.

    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "cfgtest_sinks.py").write_text(
        textwrap.dedent(
            """
            from wrapture import Sink

            class Probe(Sink):
                def __init__(self, tag):
                    self.tag = tag

            def make_sink(tag="default"):
                return Probe(tag)
            """
        )
    )

    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            pythonpath = "ops"
            capture = "summary"
            sample = 0.5

            [[observe]]
            target = "{__name__}:OrderService"
            match = "*"
            exclude = "_*"

            [[observe]]
            target = "{__name__}"
            name = ["parse_widget"]

            [sink]
            type = "cfgtest_sinks:make_sink"
            tag = "from-config"

            [[setup]]
            module = "{__name__}"
            call = "{__name__}:_note_setup"
            """
        )
    )

    monkeypatch.setattr(sys, "path", list(sys.path))

    config = load_config(source)

    assert sys.path[0] == str(ops)

    first, second = config.observe
    assert first.target == f"{__name__}:OrderService"
    assert first.match == ("*",)
    assert first.exclude == ("_*",)
    assert second.name == ("parse_widget",)

    assert type(config.sink).__name__ == "Probe"
    assert getattr(config.sink, "tag", None) == "from-config"

    (setup,) = config.setup
    assert setup.module == __name__
    assert setup.call == f"{__name__}:_note_setup"

    assert config.capture == "summary"
    assert config.sample == 0.5


def test_load_config_builds_builtin_sinks(tmp_path: Path) -> None:
    # The path is embedded with forward slashes: in a TOML basic
    # string a backslash starts an escape sequence, so the native
    # Windows form would not even parse.

    source = tmp_path / "trace.toml"
    source.write_text(
        f'[sink]\ntype = "jsonlines"\npath = "{(tmp_path / "out.jsonl").as_posix()}"\n'
    )

    config = load_config(source)

    assert isinstance(config.sink, JSONLines)


def test_an_unknown_builtin_sink_fails_loudly(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text('[sink]\ntype = "carrier-pigeon"\n')

    with pytest.raises(ConfigError, match="not a builtin sink"):
        load_config(source)


def test_a_factory_must_return_a_sink(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(f'[sink]\ntype = "{__name__}:parse_widget"\ntext = "x"\n')

    with pytest.raises(ConfigError, match="not a Sink"):
        load_config(source)


def test_unknown_keys_fail_loudly(tmp_path: Path) -> None:
    top = tmp_path / "top.toml"
    top.write_text("observ = []\n")

    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(top)

    entry = tmp_path / "entry.toml"
    entry.write_text('[[observe]]\ntarget = "mod"\nmacth = "*"\n')

    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(entry)

    setup = tmp_path / "setup.toml"
    setup.write_text('[[setup]]\nmodule = "mod"\ncall = "mod:fn"\nwhen = "now"\n')

    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(setup)


def test_invalid_toml_fails_loudly(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text("not toml at all [")

    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(source)


def test_a_missing_config_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "absent.toml")


def test_load_config_reads_the_tool_table_from_pyproject(tmp_path: Path) -> None:
    source = tmp_path / "pyproject.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "someapp"

            [tool.wrapture]
            capture = "types"

            [[tool.wrapture.observe]]
            target = "{__name__}"
            name = "parse_widget"
            """
        )
    )

    config = load_config(source)

    assert config.capture == "types"
    assert config.observe[0].name == ("parse_widget",)


def test_a_pyproject_without_the_table_fails_loudly(tmp_path: Path) -> None:
    source = tmp_path / "pyproject.toml"
    source.write_text('[project]\nname = "someapp"\n')

    with pytest.raises(ConfigError, match=r"no \[tool\.wrapture\] table"):
        load_config(source)


# ---------------------------------------------------------------------------
# source precedence
# ---------------------------------------------------------------------------


def test_find_config_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WRAPTURE_CONFIG", raising=False)

    # Nothing anywhere: no source.

    assert find_config() is None

    # A pyproject without the table is not a source; with it, it is.

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "someapp"\n')
    assert find_config() is None

    pyproject.write_text("[tool.wrapture]\n")
    assert find_config() == "pyproject.toml"

    # wrapture.toml in the current directory beats the pyproject.

    (tmp_path / "wrapture.toml").write_text("")
    assert find_config() == "wrapture.toml"

    # The environment variable beats both, and its path is returned
    # even when the file does not exist, so loading fails loudly
    # instead of silently falling through.

    monkeypatch.setenv("WRAPTURE_CONFIG", "/nowhere/trace.toml")
    assert find_config() == "/nowhere/trace.toml"
