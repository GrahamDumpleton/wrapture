"""A whole tracing setup as one value: the config primitive and the
TOML file loader.

The programmatic form is the primitive: a Config holds observe entries
saying which members of which targets to bind, the sink events flow to,
setup callbacks to trigger on module imports, and the capture and
sampling settings, and apply() installs the lot. The TOML file is a
thin loader over it; everything the file can say is expressible in
code, and the programmatic path can additionally pass live objects
where the file is limited to what TOML can spell.

Failures are loud. A file that says something the schema does not
allow, a reference that does not resolve, a named member that does not
exist: each raises ConfigError rather than being skipped. The one
softer case is a match pattern that selects nothing, which warns with
ConfigWarning, because an empty selection may simply mean the code
being observed has moved on.

A config file can name arbitrary code to run, through sink factories
and setup callbacks, so loading one is equivalent to executing code:
the trust boundary is write access to the file, exactly as it is for
any other file the process imports.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
import tomllib
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

import wrapt

from .bindings import Binding, binding
from .capture import CapturePolicy, _resolve_policy
from .exceptions import ConfigError, ConfigWarning
from .sinks import JSONLines, Printer, Sample, Sink, add_sink, remove_sink


def _strings(value: Any, *, key: str, where: str) -> tuple[str, ...]:
    # Normalise the string-or-list-of-strings shape every selection key
    # accepts, rejecting anything else loudly.

    if isinstance(value, str):
        return (value,)

    if isinstance(value, Sequence) and all(isinstance(item, str) for item in value):
        return tuple(value)

    raise ConfigError(
        f"{where}: {key} must be a string or a list of strings, got {value!r}"
    )


def _split_reference(reference: Any, *, key: str, where: str) -> tuple[str, str]:
    # Validate the module:attr reference shape without importing
    # anything, so a malformed reference fails at load even when its
    # resolution is deferred.

    if not isinstance(reference, str):
        raise ConfigError(
            f"{where}: {key} must be a module:attr string, got {reference!r}"
        )

    module, sep, attr = reference.partition(":")

    if not sep or not module or not attr:
        raise ConfigError(
            f"{where}: {key} must name a module and an attribute as"
            f" module:attr, got {reference!r}"
        )

    return module, attr


def _resolve_reference(reference: str, *, key: str, where: str) -> Any:
    # Import the module half and walk the dotted attribute half. Both
    # kinds of failure surface as ConfigError naming the reference, so
    # the message points at the config rather than at wrapture.

    module_name, attr_path = _split_reference(reference, key=key, where=where)

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(
            f"{where}: cannot import module {module_name!r}"
            f" named by {key} = {reference!r}: {exc}"
        ) from exc

    resolved: Any = module
    for part in attr_path.split("."):
        try:
            resolved = getattr(resolved, part)
        except AttributeError:
            raise ConfigError(
                f"{where}: module {module_name!r} has no attribute"
                f" {attr_path!r} named by {key} = {reference!r}"
            ) from None

    return resolved


@dataclass(frozen=True)
class ObserveEntry:
    """One observation rule: which members of one exact target to bind.

    `target` is never a pattern: "module" or "module:path", the same
    colon convention event paths use. Members within it are selected by
    `name` (exact, each must exist, binds anything including
    properties) or `match` (fnmatchcase patterns over the target's own
    immediate members, routines only), with `exclude` patterns
    subtracting from a match. Each selection field accepts one string
    or a list; `name` and `match` are mutually exclusive and one is
    required, and `exclude` only accompanies `match`.
    """

    target: str
    name: str | Sequence[str] = ()
    match: str | Sequence[str] = ()
    exclude: str | Sequence[str] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target:
            raise ConfigError(
                f"observe entry: target must be a non-empty string of the"
                f" form 'module' or 'module:path', got {self.target!r}"
            )

        where = f"observe target {self.target!r}"

        module, sep, path = self.target.partition(":")
        if not module or (sep and not path) or ":" in path:
            raise ConfigError(
                f"{where}: target must be 'module' or 'module:path',"
                f" with a single colon"
            )

        # Normalise the selection fields to tuples, then check the
        # combination rules: name XOR match, exclude only with match.

        for key in ("name", "match", "exclude"):
            value = getattr(self, key)
            object.__setattr__(
                self, key, _strings(value, key=key, where=where) if value else ()
            )

        if self.name and self.match:
            raise ConfigError(
                f"{where}: name and match are mutually exclusive; use name"
                f" for exact members or match for a pattern, not both"
            )

        if not self.name and not self.match:
            raise ConfigError(f"{where}: one of name or match is required")

        if self.exclude and not self.match:
            raise ConfigError(
                f"{where}: exclude only applies to match; with name, simply"
                f" leave the member out"
            )


@dataclass(frozen=True)
class SetupEntry:
    """One setup callback: call `call` (a module:attr reference) with
    the module named by `module` once that module is imported, or
    immediately if it already was.

    The callback reference is resolved only when the hook fires, so
    naming operator code here cannot cause it to be imported before
    the module it wants to instrument.
    """

    module: str
    call: str

    def __post_init__(self) -> None:
        if not isinstance(self.module, str) or not self.module:
            raise ConfigError(
                f"setup entry: module must be a non-empty module name,"
                f" got {self.module!r}"
            )

        _split_reference(self.call, key="call", where=f"setup for {self.module!r}")


class AppliedConfig:
    """The record of what one Config.apply() installed: every binding
    it applied, and the process sink it registered, if any."""

    def __init__(self, bindings: tuple[Binding, ...], sink: Sink | None) -> None:
        self._bindings = bindings
        self._sink = sink

    @property
    def bindings(self) -> tuple[Binding, ...]:
        """The bindings the config applied, in application order."""

        return self._bindings

    @property
    def sink(self) -> Sink | None:
        """The process sink the config registered, as registered: with
        sampling configured this is the wrapping Sample."""

        return self._sink

    def __repr__(self) -> str:
        return f"<AppliedConfig: {len(self._bindings)} bindings, sink {self._sink!r}>"


class Config:
    """A whole tracing setup: what to observe, where events go, which
    setup callbacks to trigger, and how much to capture and keep.

    This is the programmatic primitive beneath the TOML config file:
    everything a file can say is expressible here directly. Construct
    one and call apply() to install the lot, or let load_config()
    build one from a file.
    """

    def __init__(
        self,
        *,
        observe: Sequence[ObserveEntry] = (),
        sink: Sink | None = None,
        setup: Sequence[SetupEntry] = (),
        capture: CapturePolicy | str | None = None,
        sample: float | None = None,
    ) -> None:
        """Validate and hold a tracing setup, applying nothing yet.

        `observe` is the ObserveEntry rules to bind. `sink` is the
        process sink events flow to. `setup` is the SetupEntry
        callbacks to register. `capture` overrides the capture level
        on every binding the config creates, in the forms binding()
        accepts. `sample` keeps only that fraction of call trees, by
        wrapping the sink in Sample at apply time, so it requires a
        sink.
        """

        for entry in observe:
            if not isinstance(entry, ObserveEntry):
                raise ConfigError(
                    f"observe entries must be ObserveEntry instances, got {entry!r}"
                )

        for setup_entry in setup:
            if not isinstance(setup_entry, SetupEntry):
                raise ConfigError(
                    f"setup entries must be SetupEntry instances, got {setup_entry!r}"
                )

        if sink is not None and not isinstance(sink, Sink):
            raise ConfigError(f"sink must be a Sink, got {sink!r}")

        if capture is not None:
            try:
                _resolve_policy(capture)
            except ValueError as exc:
                raise ConfigError(f"capture: {exc}") from None

        if sample is not None:
            if not isinstance(sample, (int, float)) or isinstance(sample, bool):
                raise ConfigError(
                    f"sample must be a number between 0.0 and 1.0, got {sample!r}"
                )
            if not 0.0 <= sample <= 1.0:
                raise ConfigError(f"sample must be between 0.0 and 1.0, got {sample!r}")
            if sink is None:
                raise ConfigError(
                    "sample requires a sink; there is nothing else for"
                    " sampling to apply to"
                )

        self._observe = tuple(observe)
        self._sink = sink
        self._setup = tuple(setup)
        self._capture = capture
        self._sample = sample

    @property
    def observe(self) -> tuple[ObserveEntry, ...]:
        """The observation rules this config holds."""

        return self._observe

    @property
    def sink(self) -> Sink | None:
        """The sink this config registers, before any Sample wrapping."""

        return self._sink

    @property
    def setup(self) -> tuple[SetupEntry, ...]:
        """The setup callback entries this config registers."""

        return self._setup

    @property
    def capture(self) -> CapturePolicy | str | None:
        """The capture override applied to every binding, or None."""

        return self._capture

    @property
    def sample(self) -> float | None:
        """The fraction of call trees kept, or None for all of them."""

        return self._sample

    def apply(self) -> AppliedConfig:
        """Install everything this config describes, returning the
        record of what was installed.

        The sink is registered as a process sink first, wrapped in
        Sample when sampling is configured; then each observe entry is
        resolved and its bindings applied, importing target modules as
        needed; then each setup callback is registered as a wrapt
        post-import hook, which fires immediately for a module already
        imported. If any part fails, whatever had already been
        installed is removed again before ConfigError propagates, so a
        failed apply leaves nothing behind.
        """

        installed: Sink | None = None
        applied: list[Binding] = []

        try:
            if self._sink is not None:
                registered = self._sink
                if self._sample is not None:
                    registered = Sample(self._sample, registered)
                installed = add_sink(registered)

            for entry in self._observe:
                for bound in _bindings_for(entry, self._capture):
                    bound.apply()
                    applied.append(bound)

            for setup_entry in self._setup:
                _register_setup(setup_entry)
        except BaseException:
            for bound in reversed(applied):
                bound.remove()
            if installed is not None:
                remove_sink(installed)
            raise

        return AppliedConfig(tuple(applied), installed)


def _resolve_container(entry: ObserveEntry) -> tuple[str, str, Any]:
    # Import the target's module half and walk its path half, giving
    # the container object whose members the entry selects from.

    module_name, _, path = entry.target.partition(":")
    where = f"observe target {entry.target!r}"

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(
            f"{where}: cannot import module {module_name!r}: {exc}"
        ) from exc

    container: Any = module
    if path:
        for part in path.split("."):
            try:
                container = getattr(container, part)
            except AttributeError:
                raise ConfigError(
                    f"{where}: module {module_name!r} has no attribute {path!r}"
                ) from None

    return module_name, path, container


def _matched_members(entry: ObserveEntry, container: Any) -> list[str]:
    # Pattern selection is deliberately confined: immediate members
    # from the container's own vars() only, never inherited and never
    # traversing into nested classes or submodules, matched with
    # fnmatchcase against the bare names. Only routines are eligible;
    # properties, other descriptors, nested classes and plain data are
    # skipped, as is anything already wrapped, and a module's imported
    # functions and classes are skipped so a module pattern selects
    # only what the module itself defines. name= is the escape hatch
    # that binds any of the skipped kinds explicitly.

    is_module = inspect.ismodule(container)

    selected: list[str] = []
    for member, value in vars(container).items():
        if not any(fnmatchcase(member, pattern) for pattern in entry.match):
            continue
        if any(fnmatchcase(member, pattern) for pattern in entry.exclude):
            continue

        if isinstance(value, wrapt.BaseObjectProxy):
            continue

        if is_module:
            eligible = (
                inspect.isroutine(value)
                and getattr(value, "__module__", None) == container.__name__
            )
        else:
            eligible = isinstance(
                value, (staticmethod, classmethod)
            ) or inspect.isfunction(value)

        if eligible:
            selected.append(member)

    return selected


def _bindings_for(
    entry: ObserveEntry, capture: CapturePolicy | str | None
) -> list[Binding]:
    # Turn one observe entry into unapplied bindings, resolving the
    # target and selecting members per the entry's rules.

    module_name, path, container = _resolve_container(entry)
    where = f"observe target {entry.target!r}"

    if entry.name:
        for member in entry.name:
            if not hasattr(container, member):
                raise ConfigError(
                    f"{where}: no member named {member!r}; name entries must exist"
                )
        members = list(entry.name)
    else:
        members = _matched_members(entry, container)
        if not members:
            warnings.warn(
                f"{where}: match {list(entry.match)!r} selected no"
                f" members; nothing was bound for this entry",
                ConfigWarning,
                stacklevel=3,
            )

    prefix = f"{path}." if path else ""
    return [
        binding(module_name, prefix + member, capture=capture) for member in members
    ]


def _register_setup(entry: SetupEntry) -> None:
    # The trampoline defers resolving the callback reference to the
    # moment the hook fires: by then the trigger module is mid-import
    # anyway, so importing operator code cannot defeat
    # patch-before-import ordering. wrapt fires the hook immediately
    # when the module is already imported, so an entry never silently
    # waits forever on a module that is already there.

    def trampoline(module: Any) -> None:
        callback = _resolve_reference(
            entry.call, key="call", where=f"setup for {entry.module!r}"
        )

        if not callable(callback):
            raise ConfigError(
                f"setup for {entry.module!r}: call = {entry.call!r}"
                f" resolved to {callback!r}, which is not callable"
            )

        callback(module)

    wrapt.register_post_import_hook(trampoline, entry.module)


# The sinks a config file can name without a module:attr reference.
# Deliberately only the sinks that are useful with no Python in play:
# the in-memory sinks and the combinators need code to consume or
# compose them, and a factory reference covers those.

_BUILTIN_SINKS: dict[str, Callable[..., Sink]] = {
    "printer": Printer,
    "jsonlines": JSONLines,
}


def _build_sink(table: Any) -> Sink:
    # Construct the sink a [sink] table describes: a builtin short
    # name or a module:attr factory, called with the remaining keys as
    # keyword arguments, resolved and validated now because a sink
    # must exist before events can flow.

    if not isinstance(table, dict):
        raise ConfigError(f"[sink] must be a table, got {table!r}")

    spec = dict(table)
    reference = spec.pop("type", None)

    if not isinstance(reference, str) or not reference:
        raise ConfigError(
            "[sink] requires a type key naming a builtin sink or a module:attr factory"
        )

    if ":" in reference:
        factory = _resolve_reference(reference, key="type", where="[sink]")
        if not callable(factory):
            raise ConfigError(
                f"[sink]: type = {reference!r} resolved to {factory!r},"
                f" which is not callable"
            )
    else:
        try:
            factory = _BUILTIN_SINKS[reference]
        except KeyError:
            raise ConfigError(
                f"[sink]: type {reference!r} is not a builtin sink"
                f" (one of {sorted(_BUILTIN_SINKS)}) or a module:attr"
                f" factory"
            ) from None

    try:
        sink = factory(**spec)
    except Exception as exc:
        raise ConfigError(
            f"[sink]: constructing type {reference!r} failed: {exc}"
        ) from exc

    if not isinstance(sink, Sink):
        raise ConfigError(
            f"[sink]: type {reference!r} returned {type(sink).__name__!r}, not a Sink"
        )

    return sink


def _entry_table(
    table: Any, *, section: str, required: tuple[str, ...], optional: tuple[str, ...]
) -> dict[str, Any]:
    # Validate one array-of-tables entry: a table, required keys
    # present, no keys beyond the known ones.

    if not isinstance(table, dict):
        raise ConfigError(f"{section} entries must be tables, got {table!r}")

    unknown = sorted(set(table) - set(required) - set(optional))
    if unknown:
        raise ConfigError(f"{section}: unknown keys {unknown}")

    for key in required:
        if key not in table:
            raise ConfigError(f"{section}: the {key} key is required")

    return dict(table)


def _config_from(document: Any, location: str) -> Config:
    # Build a Config from a parsed TOML document, applying the one
    # load-time side effect first: pythonpath entries go onto sys.path
    # before any reference resolves, so the references may name code
    # those directories provide.

    if not isinstance(document, dict):
        raise ConfigError(f"{location}: config must be a TOML table")

    unknown = sorted(
        set(document) - {"pythonpath", "capture", "sample", "observe", "sink", "setup"}
    )
    if unknown:
        raise ConfigError(f"{location}: unknown config keys {unknown}")

    # Relative pythonpath entries anchor to the config file's own
    # directory, not the process working directory, so a config that
    # ships next to its operator code stays self-contained wherever
    # the process launches from. Prepended in order, ahead of
    # everything already on the path.

    if "pythonpath" in document:
        anchor = os.path.dirname(os.path.abspath(location))
        entries = _strings(document["pythonpath"], key="pythonpath", where=location)

        for directory in reversed(entries):
            if not os.path.isabs(directory):
                directory = os.path.normpath(os.path.join(anchor, directory))
            sys.path.insert(0, directory)

    observe: list[ObserveEntry] = []
    for raw in document.get("observe", ()):
        table = _entry_table(
            raw,
            section="[[observe]]",
            required=("target",),
            optional=("name", "match", "exclude"),
        )
        observe.append(ObserveEntry(**table))

    setup: list[SetupEntry] = []
    for raw in document.get("setup", ()):
        table = _entry_table(
            raw, section="[[setup]]", required=("module", "call"), optional=()
        )
        setup.append(SetupEntry(**table))

    sink = _build_sink(document["sink"]) if "sink" in document else None

    return Config(
        observe=observe,
        sink=sink,
        setup=setup,
        capture=document.get("capture"),
        sample=document.get("sample"),
    )


def load_config(path: str | os.PathLike[str]) -> Config:
    """Load a TOML config file into a Config, without applying it.

    The file is either a wrapture config file, whose whole content is
    the config, or a pyproject.toml, whose [tool.wrapture] table is.
    Loading has two immediate effects beyond parsing: pythonpath
    entries are prepended to sys.path, and the [sink] table's sink is
    constructed, both because later stages depend on them existing.
    Anything the schema does not allow raises ConfigError.
    """

    location = os.fspath(path)

    try:
        with open(location, "rb") as stream:
            document: Any = tomllib.load(stream)
    except OSError as exc:
        raise ConfigError(f"cannot read config file {location}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{location} is not valid TOML: {exc}") from exc

    if os.path.basename(location) == "pyproject.toml":
        document = document.get("tool", {}).get("wrapture")
        if document is None:
            raise ConfigError(f"{location} has no [tool.wrapture] table")

    return _config_from(document, location)


def find_config() -> str | None:
    """Locate the config file the environment nominates, or None.

    The precedence chain: a path in the WRAPTURE_CONFIG environment
    variable; wrapture.toml in the current directory; pyproject.toml
    in the current directory when it contains a [tool.wrapture] table.
    The first source found wins outright, and sources are never
    merged. A nominated path that does not exist is returned anyway,
    so loading it fails loudly rather than silently falling through
    to a lower-precedence source.
    """

    nominated = os.environ.get("WRAPTURE_CONFIG")
    if nominated:
        return nominated

    if os.path.isfile("wrapture.toml"):
        return "wrapture.toml"

    # pyproject.toml counts as a source only when the table is
    # actually present, which takes parsing it to know; a pyproject
    # that does not parse is reported rather than skipped.

    if os.path.isfile("pyproject.toml"):
        try:
            with open("pyproject.toml", "rb") as stream:
                document = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"pyproject.toml is not valid TOML: {exc}") from exc

        if document.get("tool", {}).get("wrapture") is not None:
            return "pyproject.toml"

    return None
