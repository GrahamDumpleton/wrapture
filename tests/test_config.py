"""Tests for the config layer: the programmatic Config primitive and
the TOML file loader.

A Config bundles observe entries, a sink, setup callbacks, and the
capture and sampling settings; apply() installs the lot and unwinds
itself on failure. The TOML loader is a thin schema over the same
primitive, with loud failures for anything it does not understand.
"""

import importlib
import os
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path
from typing import Any

import pytest
import wrapt

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
    applied.revert()


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


def test_an_unimported_target_stays_pending() -> None:
    # Deferral cannot tell a misspelled module from one not imported
    # yet, so applying succeeds and the entry waits; the pending view
    # and the shutdown report are where this surfaces.

    config = Config(
        observe=[ObserveEntry(target="no_such_module_anywhere:Thing", name="run")]
    )

    applied = config.apply()
    try:
        assert applied.bindings == ()

        (entry,) = applied.pending
        assert entry.target == "no_such_module_anywhere:Thing"
        assert "no_such_module_anywhere:Thing" in applied.report()
    finally:
        _unwind(applied)


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
        importlib.import_module("cfgtest_widgets")
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
# deferral
# ---------------------------------------------------------------------------


def test_bindings_arrive_when_the_target_module_is_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cfg15_lazy_mod.py").write_text("def run(task):\n    return task\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target="cfg15_lazy_mod", name="run")],
        sink=collector,
    )

    applied = config.apply()
    try:
        # Nothing imported, nothing bound: the entry waits.

        assert applied.bindings == ()
        (entry,) = applied.pending
        assert entry.target == "cfg15_lazy_mod"

        # The application's own import is the trigger.

        module = importlib.import_module("cfg15_lazy_mod")

        arrived: tuple[Any, ...] = applied.bindings
        (bound,) = arrived
        assert bound.name == "run"
        assert applied.pending == ()

        module.run("job")
        assert [event.path for event in collector.entered] == ["cfg15_lazy_mod:run"]
    finally:
        _unwind(applied)


def test_fire_time_validation_warns_and_the_import_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Validation that needs the module (here a name that must exist)
    # cannot run until the module arrives; fired inside an application
    # import, the failure must not fail that import, so the entry is
    # dropped with a warning and the module comes through usable.

    (tmp_path / "cfg15_bad_mod.py").write_text("value = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    config = Config(observe=[ObserveEntry(target="cfg15_bad_mod", name="missing")])

    applied = config.apply()
    try:
        with pytest.warns(ConfigWarning, match="no member named 'missing'"):
            module = importlib.import_module("cfg15_bad_mod")

        assert module.value == 1
        assert applied.bindings == ()
        assert applied.pending == ()
    finally:
        _unwind(applied)


def test_revert_neutralises_hooks_that_have_not_fired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A wrapt hook cannot be unregistered, so reverting marks the
    # record instead: the module's later import applies nothing.

    (tmp_path / "cfg15_reverted_mod.py").write_text("def run(task):\n    return task\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target="cfg15_reverted_mod", name="run")],
        sink=collector,
    )

    applied = config.apply()
    applied.revert()
    applied.revert()

    module = importlib.import_module("cfg15_reverted_mod")

    assert applied.bindings == ()
    assert not isinstance(module.run, wrapt.BaseObjectProxy)

    module.run("job")
    assert collector.entered == []


LAZY_PROBE = """
import sys
import wrapture

class Collector(wrapture.Sink):
    def __init__(self):
        self.entered = []
    def on_enter(self, event):
        self.entered.append(event)

collector = Collector()
applied = wrapture.Config(
    observe=[wrapture.ObserveEntry(target="lazy_target", name="ping")],
    sink=collector,
).apply()

lazy import lazy_target

assert "lazy_target" not in sys.modules
assert applied.bindings == ()

assert lazy_target.ping("x") == "pong:x"

assert [bound.name for bound in applied.bindings] == ["ping"]
assert [event.path for event in collector.entered] == ["lazy_target:ping"]
print("lazy-import-deferral-ok")
"""


@pytest.mark.skipif(
    sys.version_info < (3, 15), reason="lazy import syntax arrived in 3.15"
)
def test_deferral_fires_when_a_lazy_import_reifies(tmp_path: Path) -> None:
    # The lazy import statement imports nothing, so the entry stays
    # pending; first use reifies the module through the normal import
    # machinery, so the hook fires and the binding lands before the
    # touched attribute is fetched. The 3.15-only syntax lives in a
    # subprocess script so this file parses everywhere.

    (tmp_path / "lazy_target.py").write_text(
        "def ping(text):\n    return f'pong:{text}'\n"
    )
    (tmp_path / "probe.py").write_text(LAZY_PROBE)

    completed = subprocess.run(
        [sys.executable, "probe.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert "lazy-import-deferral-ok" in completed.stdout


def test_report_lists_the_sink_applied_and_pending() -> None:
    collector = Collector()
    config = Config(
        observe=[
            ObserveEntry(target=f"{__name__}:OrderService", name="place"),
            ObserveEntry(target="cfg15_ghost_mod", name="run"),
        ],
        sink=collector,
    )

    applied = config.apply()
    try:
        text = applied.report()

        assert "Collector" in text
        assert f"  {__name__}:OrderService.place" in text
        assert "pending:" in text
        assert "  cfg15_ghost_mod" in text
    finally:
        _unwind(applied)


def test_the_shutdown_report_names_never_imported_targets() -> None:
    from wrapture.config import _report_never_fired

    config = Config(observe=[ObserveEntry(target="cfg15_ghost2_mod", name="run")])

    applied = config.apply()
    try:
        with pytest.warns(ConfigWarning, match="cfg15_ghost2_mod"):
            _report_never_fired()
    finally:
        _unwind(applied)

    # A reverted record has nothing outstanding to report.

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _report_never_fired()


# ---------------------------------------------------------------------------
# operational hardening
# ---------------------------------------------------------------------------


def test_a_tape_is_rejected_as_a_config_sink() -> None:
    # A Tape retains every event; a config sink lives for the life of
    # the process, so the combination is refused however deeply a
    # composition buries it.

    from wrapture import Depth, Fanout, Printer, Tape

    with pytest.raises(ConfigError, match="Tape retains every event"):
        Config(sink=Tape())

    with pytest.raises(ConfigError, match="Tape retains every event"):
        Config(sink=Fanout(Printer(), Depth(2, Tape())))


def test_redact_replaces_named_parameters_on_the_entrys_bindings() -> None:
    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target=__name__, name="parse_widget", redact="text")],
        sink=collector,
    )

    applied = config.apply()
    try:
        parse_widget("secret-token")
    finally:
        _unwind(applied)

    (event,) = collector.entered
    assert event.arguments == {"text": "<redacted>"}


def test_the_loader_accepts_a_redact_key(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[observe]]
            target = "{__name__}"
            name = "parse_widget"
            redact = ["text"]
            """
        )
    )

    config = load_config(source)

    assert config.observe[0].redact == ("text",)


def test_inherit_false_strips_only_wrapture_from_the_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOWRAPT_BOOTSTRAP", "othertool,wrapture")

    applied = Config(inherit=False).apply()
    try:
        assert os.environ["AUTOWRAPT_BOOTSTRAP"] == "othertool"
    finally:
        _unwind(applied)

    # Alone on the list, the variable goes entirely.

    monkeypatch.setenv("AUTOWRAPT_BOOTSTRAP", "wrapture")

    applied = Config(inherit=False).apply()
    try:
        assert "AUTOWRAPT_BOOTSTRAP" not in os.environ
    finally:
        _unwind(applied)


def test_inherit_defaults_to_leaving_the_environment_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOWRAPT_BOOTSTRAP", "wrapture")

    applied = Config().apply()
    try:
        assert os.environ["AUTOWRAPT_BOOTSTRAP"] == "wrapture"
    finally:
        _unwind(applied)


def test_inherit_must_be_a_bool(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="inherit must be true or false"):
        Config(inherit="no")  # type: ignore[arg-type]

    source = tmp_path / "trace.toml"
    source.write_text('inherit = "no"\n')

    with pytest.raises(ConfigError, match="inherit must be true or false"):
        load_config(source)


def test_suspend_and_resume_toggle_the_whole_config() -> None:
    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target=f"{__name__}:OrderService", name="place")],
        sink=collector,
    )

    applied = config.apply()
    try:
        applied.suspend()
        OrderService().place()
        assert collector.entered == []

        (bound,) = applied.bindings
        assert bound.suspended_calls == 1

        applied.resume()
        OrderService().place()
        assert len(collector.entered) == 1
    finally:
        _unwind(applied)


def test_a_pending_entry_firing_while_suspended_applies_suspended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cfg16_late_mod.py").write_text("def run(task):\n    return task\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    collector = Collector()
    config = Config(
        observe=[ObserveEntry(target="cfg16_late_mod", name="run")],
        sink=collector,
    )

    applied = config.apply()
    try:
        applied.suspend()

        module = importlib.import_module("cfg16_late_mod")
        module.run("job")

        assert collector.entered == []

        applied.resume()
        module.run("job")
        assert len(collector.entered) == 1
    finally:
        _unwind(applied)


def _raising_setup(module: Any) -> None:
    raise RuntimeError("operator code broke")


_setup_seen: list[tuple[Any, dict[str, Any]]] = []


def _optioned_setup(module: Any, **options: Any) -> None:
    _setup_seen.append((module, options))


def test_extra_setup_keys_reach_the_handler_as_keyword_arguments() -> None:
    del _setup_seen[:]

    config = Config(
        setup=[
            SetupEntry(
                module=__name__,
                call=f"{__name__}:_optioned_setup",
                options={"tenants": ["acme"], "limit": 3},
            )
        ]
    )
    config.apply()

    ((module, options),) = _setup_seen
    assert module is sys.modules[__name__]
    assert options == {"tenants": ["acme"], "limit": 3}


def test_the_loader_turns_extra_setup_keys_into_options(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            """
            [[setup]]
            module = "mod"
            call = "mod:fn"
            tenants = ["acme"]
            limit = 3
            """
        )
    )

    config = load_config(source)

    (entry,) = config.setup
    assert entry.options == {"tenants": ["acme"], "limit": 3}


def test_a_setup_group_registers_every_declared_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The group's entry points are discovered from metadata (faked
    # here with real EntryPoint objects, so resolution is the real
    # machinery); each entry hooks its own module, and every handler
    # in the family receives the entry's options.

    from importlib.metadata import EntryPoint
    from types import SimpleNamespace

    (tmp_path / "cfg17_ga_mod.py").write_text("MARK = 'a'\n")
    (tmp_path / "cfg17_gb_mod.py").write_text("MARK = 'b'\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    points = [
        EntryPoint("cfg17_ga_mod", f"{__name__}:_optioned_setup", "cfg17_hooks"),
        EntryPoint("cfg17_gb_mod", f"{__name__}:_optioned_setup", "cfg17_hooks"),
    ]
    monkeypatch.setattr(
        "wrapture.config.metadata",
        SimpleNamespace(entry_points=lambda group: points),
    )

    del _setup_seen[:]

    config = Config(setup=[SetupEntry(group="cfg17_hooks", options={"headers": False})])
    config.apply()

    assert _setup_seen == []

    importlib.import_module("cfg17_ga_mod")
    importlib.import_module("cfg17_gb_mod")

    marks = [(module.MARK, options) for module, options in _setup_seen]
    assert marks == [("a", {"headers": False}), ("b", {"headers": False})]


def test_an_empty_setup_group_fails_loudly() -> None:
    config = Config(setup=[SetupEntry(group="definitely_not_a_group_xyz")])

    with pytest.raises(ConfigError, match="has no entry points"):
        config.apply()


def test_group_and_the_single_form_are_mutually_exclusive() -> None:
    with pytest.raises(ConfigError, match="alternative to module and call"):
        SetupEntry(module="mod", call="mod:fn", group="hooks")


def test_setup_options_must_be_a_mapping() -> None:
    with pytest.raises(ConfigError, match="options must be a mapping"):
        SetupEntry(module="mod", call="mod:fn", options="loud")  # type: ignore[arg-type]


def test_a_setup_callback_raising_at_deferred_fire_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # During apply a raising callback is the caller's to hear; fired
    # from an application import later, it warns and the import
    # continues, since observation must never break the application.

    (tmp_path / "cfg16_setup_mod.py").write_text("value = 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    config = Config(
        setup=[SetupEntry(module="cfg16_setup_mod", call=f"{__name__}:_raising_setup")]
    )

    applied = config.apply()
    try:
        with pytest.warns(ConfigWarning, match="operator code broke"):
            module = importlib.import_module("cfg16_setup_mod")

        assert module.value == 2
    finally:
        _unwind(applied)


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

            [[observe]]
            target = "{__name__}:OrderService"
            match = "*"
            exclude = "_*"

            [[observe]]
            target = "{__name__}"
            name = ["parse_widget"]

            [[sink]]
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


def test_load_config_builds_builtin_sinks(tmp_path: Path) -> None:
    # The path is embedded with forward slashes: in a TOML basic
    # string a backslash starts an escape sequence, so the native
    # Windows form would not even parse.

    source = tmp_path / "trace.toml"
    out = (tmp_path / "out.jsonl").as_posix()
    source.write_text(f'[[sink]]\ntype = "jsonlines"\npath = "{out}"\n')

    config = load_config(source)

    assert isinstance(config.sink, JSONLines)


def test_load_config_passes_printer_options_through(tmp_path: Path) -> None:
    from wrapture import Printer

    source = tmp_path / "trace.toml"
    source.write_text(
        '[[sink]]\ntype = "printer"\n'
        f'path = "{(tmp_path / "trace.log").as_posix()}"\n'
        "timestamps = true\ntiming = false\n"
    )

    config = load_config(source)

    # Compared as paths: the TOML spells the path with forward slashes,
    # which Windows accepts but does not print back.

    assert isinstance(config.sink, Printer)
    assert config.sink._path is not None
    assert Path(config.sink._path.template) == tmp_path / "trace.log"
    assert config.sink._timestamps is True
    assert config.sink._timing is False


def test_a_relative_sink_path_anchors_to_the_config_file(tmp_path: Path) -> None:
    # As pythonpath entries do: the file says where its output goes
    # regardless of the process's working directory, and only the
    # directory part is anchored, the template staying intact.

    home = tmp_path / "deploy"
    home.mkdir()
    source = home / "trace.toml"
    source.write_text(
        '[[sink]]\ntype = "jsonlines"\npath = "logs/trace-{date}.jsonl"\n'
    )

    config = load_config(source)

    assert isinstance(config.sink, JSONLines)
    assert config.sink._path.template == str(home / "logs" / "trace-{date}.jsonl")


def test_a_bad_sink_path_template_is_a_config_error(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text('[[sink]]\ntype = "jsonlines"\npath = "trace-{seq}.jsonl"\n')

    with pytest.raises(ConfigError, match="unknown variable {seq}"):
        load_config(source)


# Module-level helpers the sink grammar tests reference from TOML by
# module:attr, so nothing has to be written to disk.

_made: list[Any] = []


class OptionCollector(Collector):
    def __init__(self, **options: Any) -> None:
        super().__init__()
        self.options = options


def make_collector(**options: Any) -> Sink:
    collector = OptionCollector(**options)
    _made.append(collector)
    return collector


class Router(Sink):
    def __init__(self, to: list[Sink], **options: Any) -> None:
        self.to = to
        self.options = options


def make_router(**options: Any) -> Sink:
    router = Router(**options)
    _made.append(router)
    return router


def only_requests(event: Event) -> bool:
    return event.kind == "request"


def test_several_sink_entries_fan_out(tmp_path: Path) -> None:
    from wrapture import Fanout, Printer

    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[sink]]
            type = "printer"

            [[sink]]
            type = "{__name__}:make_collector"
            tag = "second"
            """
        )
    )

    config = load_config(source)

    assert isinstance(config.sink, Fanout)
    printer, collector = config.sink._sinks
    assert isinstance(printer, Printer)
    assert isinstance(collector, OptionCollector)
    assert collector.options == {"tag": "second"}


def test_a_single_sink_table_is_named_in_the_error(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text('[sink]\ntype = "printer"\n')

    with pytest.raises(ConfigError, match=r"write each destination as a \[\[sink\]\]"):
        load_config(source)


def test_gating_keys_wrap_the_sink_in_a_fixed_order(tmp_path: Path) -> None:
    # sample outermost, then depth, then filter, whatever the order of
    # the keys in the file.

    from wrapture import Depth, Filter

    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[sink]]
            filter = {{ kind = "request" }}
            depth = 2
            type = "{__name__}:make_collector"
            sample = 0.5
            """
        )
    )

    config = load_config(source)

    assert isinstance(config.sink, Sample)
    depth = config.sink._sink
    assert isinstance(depth, Depth)
    filtered = depth._sink
    assert isinstance(filtered, Filter)
    assert isinstance(filtered._sink, Collector)


def test_a_filter_table_matches_event_fields(tmp_path: Path) -> None:
    from wrapture import Filter

    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[sink]]
            type = "{__name__}:make_collector"
            filter = {{ kind = ["call", "get"], path = "shop.*" }}
            """
        )
    )

    config = load_config(source)

    assert isinstance(config.sink, Filter)
    predicate = config.sink._predicate

    assert predicate(Event(kind="call", path="shop.Gateway.charge"))
    assert predicate(Event(kind="get", path="shop.Gateway.limit"))
    assert not predicate(Event(kind="request", path="shop.app"))
    assert not predicate(Event(kind="call", path="billing.charge"))
    assert not predicate(Event(kind="call", path="billing.charge", label="shop.x"))


def test_a_filter_reference_names_a_predicate(tmp_path: Path) -> None:
    from wrapture import Filter

    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[sink]]
            type = "{__name__}:make_collector"
            filter = "{__name__}:only_requests"
            """
        )
    )

    config = load_config(source)

    assert isinstance(config.sink, Filter)
    assert config.sink._predicate is only_requests


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ("sample = 2", "sample must be a number between 0.0 and 1.0"),
        ("sample = true", "sample must be a number between 0.0 and 1.0"),
        ("depth = 0", "depth must be a positive integer"),
        ('filter = { thread = "x" }', "filter field 'thread' is not one of"),
        ('filter = "nonsense"', "filter must name a module and an attribute"),
        ("filter = 3", "filter must be a table of event fields"),
        ("to = []", "to only applies to a module:attr factory"),
    ],
)
def test_bad_gating_keys_fail_at_load(tmp_path: Path, keys: str, message: str) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(f'[[sink]]\ntype = "printer"\n{keys}\n')

    with pytest.raises(ConfigError, match=message):
        load_config(source)


def test_a_factory_takes_inner_sinks_under_to(tmp_path: Path) -> None:
    # The one nesting case: a routing factory receives its inner sinks,
    # each built with the same grammar and gating keys, as to=[...].

    from wrapture import Depth, Printer

    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            f"""
            [[sink]]
            type = "{__name__}:make_router"
            tag = "outer"

            [[sink.to]]
            type = "printer"
            depth = 1

            [[sink.to]]
            type = "{__name__}:make_collector"
            filter = {{ kind = "request" }}
            """
        )
    )

    config = load_config(source)

    assert isinstance(config.sink, Router)
    assert config.sink.options == {"tag": "outer"}
    printer, collector = config.sink.to
    assert isinstance(printer, Depth)
    assert isinstance(printer._sink, Printer)
    assert type(collector).__name__ == "Filter"


def test_top_level_sample_is_no_longer_a_key(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text('sample = 0.5\n[[sink]]\ntype = "printer"\n')

    with pytest.raises(ConfigError, match=r"unknown config keys \['sample'\]"):
        load_config(source)


def test_an_unknown_builtin_sink_fails_loudly(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text('[[sink]]\ntype = "carrier-pigeon"\n')

    with pytest.raises(ConfigError, match="not a builtin sink"):
        load_config(source)


def test_a_factory_must_return_a_sink(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(f'[[sink]]\ntype = "{__name__}:parse_widget"\ntext = "x"\n')

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
