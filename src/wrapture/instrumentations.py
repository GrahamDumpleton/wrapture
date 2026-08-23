"""Instrumentation packages: a self-describing contract for code that
patches one target package on wrapture's behalf.

A package (or a module next to a config file) ships an Instrumentation
subclass: class data says what it is and covers, apply() patches one
trigger module once that module is imported, and remove() undoes it.
wrapture discovers subclasses through the `wrapture.instrumentation`
entry point group or by module:attr reference, validates them, applies
them from an [[instrument]] config entry or the instrumentation()
context manager, reports on them, and removes them on revert, without
knowing where the hook code lives or importing it ahead of the target.

The module that defines a subclass imports wrapture and nothing else;
everything that touches the target lives behind an import inside
apply() and remove(), so loading the class never defeats
patch-before-import for the very modules it claims.
"""

from __future__ import annotations

import importlib
import sys
import threading
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import wrapt
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .exceptions import ConfigError, ConfigWarning

if TYPE_CHECKING:
    from .config import AppliedConfig, Config

# The entry point group an installed package registers its classes in,
# one entry per class, the entry point name being the instrumentation's
# name: `flask = "wrapture_instrumentation_flask:FlaskInstrumentation"`.

GROUP = "wrapture.instrumentation"


class Setting:
    """One declared setting of an instrumentation: its default and a
    one-line description.

    The declaration is what validates the keys of an [[instrument]]
    entry (an unknown key is a ConfigError, as is a value whose outer
    type does not match the default's), fills in defaults, and feeds
    the listing tool's settings table and the generated TOML template.
    """

    __slots__ = ("default", "description")

    def __init__(self, default: Any, description: str = "") -> None:
        if not isinstance(description, str):
            raise TypeError(
                f"Setting description must be a string, got {description!r}"
            )

        self.default = default
        self.description = description

    def __repr__(self) -> str:
        return f"Setting({self.default!r}, {self.description!r})"


def _summary(cls: type) -> str:
    # The first line of the class docstring, the local default for an
    # instrumentation's description.

    doc = cls.__doc__ or ""
    for line in doc.strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


class Instrumentation:
    """Instrumentation for one target package, shipped as a subclass.

    Class data declares what the instrumentation is and covers: the
    `target` import name, the trigger `modules` under it, the version
    range it `supports`, other targets it `requires`, whether it is
    `removable`, and the `settings` it takes. The instance is
    wrapture's per-application record, handed to the hooks as self:
    `apply(name, module)` is called once per trigger module when that
    module is imported, `remove(name, module)` undoes it. The module
    defining a subclass imports only wrapture; anything touching the
    target is imported inside apply() and remove().
    """

    # -- class data the subclass declares ------------------------------

    # Identity. Each defaults when left empty: name from the entry point
    # name (or the target, for a class named by reference), description
    # from the distribution's summary (or the class docstring's first
    # line), version from the distribution (or none, for a local class).

    name: str = ""
    description: str = ""
    version: str = ""

    # Coverage. target is exactly one top-level import name and every
    # trigger module must live under it; supports is a PEP 440
    # specifier against the target's installed version; modules may be
    # a mapping carrying a per-module specifier for a module that only
    # exists from some version on; requires names other targets that
    # must have an active instrumentation in the same config.

    target: str = ""
    supports: str = ""
    modules: tuple[str, ...] | Mapping[str, str | None] = ()
    requires: tuple[str, ...] = ()

    # The claim report() and revert() trust: a package must say it can
    # undo itself. Callbacks registered with on_remove() run either way.

    removable: bool = False

    # The declaration, name to Setting. The resolved values take the
    # same name on the instance, an ordinary attribute __init__ assigns
    # that shadows this one for instance access.

    settings: Mapping[str, Any] = {}

    # -- construction: wrapture's, not the subclass's -------------------

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _check_class(cls)

    def __init__(self, **settings: Any) -> None:
        """Build the per-application record, with the given settings
        resolved over the declared defaults.

        wrapture constructs one per application of the instrumentation;
        a package's own tests construct one directly. An unknown setting
        raises ConfigError, as does a value whose outer type does not
        match its default's; declared settings not given take their
        defaults. Not meant to be overridden: one-time work goes in
        configure().
        """

        cls = type(self)
        where = f"instrumentation {cls.__name__}"

        self.settings = MappingProxyType(_resolve_settings(cls, settings, where))

        # Identity defaults to the local form; _identify() replaces it
        # with the entry point's when wrapture resolved the class that
        # way.

        self.name = cls.name or cls.target
        self.description = cls.description or _summary(cls)
        self.version = cls.version
        self.distribution: str | None = None

        # Bookkeeping. Hooks fire on whatever thread imports the trigger
        # module, so the lists are appended under the lock and the
        # current trigger is per-thread; the lock is never held across
        # the subclass's own apply() or remove().

        self._lock = threading.Lock()
        self._local = threading.local()
        self._applied: list[str] = []
        self._modules: dict[str, Any] = {}
        self._callbacks: list[tuple[str | None, Callable[[], Any]]] = []
        self._triggers: tuple[str, ...] = ()
        self._target_version = _target_version(cls.target)

    # -- methods the subclass overrides --------------------------------

    def configure(self) -> None:
        """Optional one-time work after construction and before any
        trigger fires: validate settings beyond their outer type (raise
        ConfigError, which surfaces at config time), register a sink,
        prepare state. The default does nothing."""

    def apply(self, name: str, module: Any) -> None:
        """Patch one trigger module. Called once per trigger, when that
        module is imported (immediately if it already was), with the
        trigger's name and the module object. Import the hook code here,
        patch through wrapture or otherwise, and register the undo with
        on_remove() unless remove() is overridden. Required."""

        raise NotImplementedError(
            f"{type(self).__name__} does not define apply(name, module)"
        )

    def remove(self, name: str, module: Any) -> None:
        """Undo one trigger's patches. Called for each trigger whose
        apply() ran, in reverse order on revert. The default runs the
        callbacks registered with on_remove() during that trigger's
        apply(), most recent first, continuing past one that raises;
        override it for a centralised teardown."""

        _run_callbacks(self._take_callbacks(name), self, name)

    # -- API available on self -----------------------------------------

    @property
    def target_version(self) -> str | None:
        """The target distribution's installed version from metadata,
        None when no distribution stands behind the target."""

        return self._target_version

    @property
    def applied(self) -> tuple[str, ...]:
        """The trigger modules whose apply() has run, in firing order."""

        with self._lock:
            return tuple(self._applied)

    @property
    def pending(self) -> tuple[str, ...]:
        """The trigger modules registered for this application whose
        module has not been imported yet."""

        with self._lock:
            return tuple(name for name in self._triggers if name not in self._applied)

    @property
    def trigger(self) -> str | None:
        """The trigger module whose apply() is running on this thread,
        so on_remove() can tag callbacks without being told; None
        outside apply()."""

        value: str | None = getattr(self._local, "trigger", None)
        return value

    def on_remove(self, callback: Callable[[], Any]) -> None:
        """Register an undo callback against the trigger currently
        applying, or against the whole instrumentation when called
        outside apply() (from configure(), say).

        Call it as many times as there are things to undo: every
        callback registered for a trigger runs from the default
        remove(), most recent first, continuing past one that raises;
        its return value is ignored, so a binding's remove() or a
        group's passes straight in. No dedupe, the same callable twice
        runs twice. Thread-safe.
        """

        if not callable(callback):
            raise TypeError(f"on_remove() takes a callable, got {callback!r}")

        with self._lock:
            self._callbacks.append((self.trigger, callback))

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} target {self.target!r}>"

    # -- internal: wrapture's side of the contract ----------------------

    def _identify(self, resolved: _Resolved) -> None:
        # Adopt the identity the resolution found: the entry point's
        # name and distribution, or the local defaults.

        self.name = resolved.name
        self.description = resolved.description
        self.version = resolved.version
        self.distribution = resolved.distribution

    def _fire(self, name: str, module: Any) -> None:
        # The trampoline target. Runs apply() with the trigger set for
        # this thread, then records the trigger; an apply() that raises
        # has the callbacks it registered run at once so its partial
        # work does not linger, and the error propagates to the caller.

        self._local.trigger = name
        try:
            self.apply(name, module)
        except BaseException:
            self._local.trigger = None
            _run_callbacks(self._take_callbacks(name), self, name)
            raise
        finally:
            self._local.trigger = None

        with self._lock:
            self._applied.append(name)
            self._modules[name] = module

    def _teardown(self) -> None:
        # Call remove() per fired trigger, most recent first, then the
        # whole-instrumentation callbacks; snapshot under the lock, run
        # outside it. A remove() that raises is warned about and the
        # teardown continues.

        with self._lock:
            names = list(reversed(self._applied))
            modules = {name: self._modules.pop(name) for name in names}
            self._applied.clear()

        for name in names:
            try:
                self.remove(name, modules[name])
            except Exception as exc:
                warnings.warn(
                    f"instrumentation {self.name!r}: remove() for {name!r}"
                    f" raised {exc!r}; continuing with the rest",
                    ConfigWarning,
                    stacklevel=3,
                )

        _run_callbacks(self._take_callbacks(None), self, None)

        with self._lock:
            self._callbacks.clear()

    def _take_callbacks(self, name: str | None) -> list[Callable[[], Any]]:
        # Remove and return the callbacks tagged with one trigger (or
        # None for the whole instrumentation), most recent first.

        with self._lock:
            taken = [cb for tag, cb in self._callbacks if tag == name]
            self._callbacks = [(tag, cb) for tag, cb in self._callbacks if tag != name]

        taken.reverse()
        return taken

    def _callback_count(self) -> int:
        with self._lock:
            return len(self._callbacks)

    def _describe(self) -> str:
        # One line for report(): identity, target and version, triggers
        # fired against pending, removability against undo callbacks.

        source = (
            f"{self.distribution} {self.version}".strip()
            if self.distribution
            else "local"
        )
        found = self.target_version
        target = f"{self.target} {found}" if found else f"{self.target} (no version)"

        parts = [f"{self.name} [{source}] target {target}"]
        applied = self.applied
        pending = self.pending
        parts.append(f"applied {', '.join(applied)}" if applied else "applied none")
        if pending:
            parts.append(f"pending {', '.join(pending)}")
        parts.append("removable" if type(self).removable else "not removable")
        parts.append(f"{self._callback_count()} undo callbacks")

        return "; ".join(parts)


def _run_callbacks(
    callbacks: Sequence[Callable[[], Any]], instance: Instrumentation, name: str | None
) -> None:
    # Warn-and-continue: one failing undo must not stop the others.

    for callback in callbacks:
        try:
            callback()
        except Exception as exc:
            scope = f"for {name!r}" if name else "for the whole instrumentation"
            warnings.warn(
                f"instrumentation {instance.name!r}: undo callback"
                f" {callback!r} {scope} raised {exc!r}; continuing",
                ConfigWarning,
                stacklevel=4,
            )


# ---------------------------------------------------------------------------
# class and settings validation
# ---------------------------------------------------------------------------


def _check_class(cls: type[Instrumentation]) -> None:
    # The static checks on a subclass, run the moment it is defined:
    # wrapture refuses the class rather than a config naming it later.

    where = f"instrumentation class {cls.__qualname__}"

    for key in ("name", "description", "version", "supports"):
        if not isinstance(getattr(cls, key), str):
            raise ConfigError(f"{where}: {key} must be a string")

    target = cls.target
    if not isinstance(target, str) or not target:
        raise ConfigError(
            f"{where}: target must name the one top-level import name the"
            f" instrumentation covers, got {target!r}"
        )
    if "." in target or ":" in target:
        raise ConfigError(
            f"{where}: target must be a top-level import name with no dots,"
            f" got {target!r}; the modules under it go in modules"
        )

    if not isinstance(cls.removable, bool):
        raise ConfigError(f"{where}: removable must be True or False")

    # Triggers: a sequence of names, or a mapping of name to optional
    # specifier; every name under the target.

    modules = cls.modules
    if isinstance(modules, Mapping):
        specifiers = dict(modules)
    elif isinstance(modules, str) or not isinstance(modules, Sequence):
        raise ConfigError(
            f"{where}: modules must be a tuple of module names or a mapping"
            f" of module name to version specifier, got {modules!r}"
        )
    else:
        specifiers = dict.fromkeys(modules)

    for name, specifier in specifiers.items():
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{where}: modules must be module names, got {name!r}")
        if name != target and not name.startswith(f"{target}."):
            raise ConfigError(
                f"{where}: trigger module {name!r} is not under target {target!r};"
                f" an instrumentation covers one target and every trigger"
                f" must live under it"
            )
        if specifier is not None:
            _check_specifier(specifier, f"{where}: modules[{name!r}]")

    if cls.supports:
        _check_specifier(cls.supports, f"{where}: supports")

    requires = cls.requires
    if isinstance(requires, str) or not isinstance(requires, Sequence):
        raise ConfigError(f"{where}: requires must be a tuple of target names")
    for required in requires:
        if not isinstance(required, str) or not required:
            raise ConfigError(
                f"{where}: requires must be target names, got {required!r}"
            )

    if not isinstance(cls.settings, Mapping):
        raise ConfigError(f"{where}: settings must be a mapping of name to Setting")
    for key, setting in cls.settings.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(f"{where}: setting names must be strings, got {key!r}")
        if not isinstance(setting, Setting):
            raise ConfigError(
                f"{where}: setting {key!r} must be declared as"
                f" wrapture.Setting(default, description), got {setting!r}"
            )


def _check_specifier(specifier: Any, where: str) -> SpecifierSet:
    if not isinstance(specifier, str):
        raise ConfigError(f"{where} must be a PEP 440 specifier string")

    try:
        return SpecifierSet(specifier)
    except InvalidSpecifier as exc:
        raise ConfigError(f"{where}: not a valid PEP 440 specifier: {exc}") from None


def _expected_type(default: Any) -> str | None:
    # The outer type a setting's default implies, as a word for the
    # error message; None when the default constrains nothing.

    if default is None:
        return None
    if isinstance(default, bool):
        return "a boolean"
    if isinstance(default, int):
        return "an integer"
    if isinstance(default, float):
        return "a number"
    if isinstance(default, str):
        return "a string"
    if isinstance(default, Mapping):
        return "a table"
    if isinstance(default, Sequence) and not isinstance(default, (str, bytes)):
        return "a list"
    return None


def _matches(default: Any, value: Any) -> bool:
    # Top level only: a scalar's type, sequence-ness, mapping-ness. An
    # int satisfies a float default; a bool never satisfies a numeric
    # one, nor the reverse.

    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, Mapping):
        return isinstance(value, Mapping)
    if isinstance(default, Sequence) and not isinstance(default, (str, bytes)):
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    return True


def _resolve_settings(
    cls: type[Instrumentation], given: Mapping[str, Any], where: str
) -> dict[str, Any]:
    # Class defaults under the supplied values: unknown names and
    # wrong outer types are loud, everything else fills in.

    declared = cls.settings

    unknown = sorted(set(given) - set(declared))
    if unknown:
        known = sorted(declared)
        hint = f"; the declared settings are {known}" if known else "; it declares none"
        raise ConfigError(f"{where}: unknown settings {unknown}{hint}")

    resolved: dict[str, Any] = {}
    for name, setting in declared.items():
        if name not in given:
            resolved[name] = setting.default
            continue

        value = given[name]
        if not _matches(setting.default, value):
            raise ConfigError(
                f"{where}: setting {name!r} expects {_expected_type(setting.default)}"
                f" (its default is {setting.default!r}), got {value!r}"
            )
        resolved[name] = value

    return resolved


# ---------------------------------------------------------------------------
# resolution: names, references and classes to an identified class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Resolved:
    # A class plus the identity its naming gave it.

    cls: type[Instrumentation]
    name: str
    description: str
    version: str
    distribution: str | None
    reference: str
    imported: tuple[str, ...] = ()

    @property
    def qualified(self) -> str:
        """The name@distribution spelling, or the bare name for a local
        class."""

        if self.distribution:
            return f"{self.name}@{self.distribution}"
        return self.name


def _registered() -> list[metadata.EntryPoint]:
    # Every entry point in the group, across all installed
    # distributions. A thin wrapper so tests can substitute.

    return list(metadata.entry_points(group=GROUP))


def _distribution_name(point: metadata.EntryPoint) -> str | None:
    dist = getattr(point, "dist", None)
    if dist is None:
        return None
    name: str | None = getattr(dist, "name", None)
    return name


def _check_loaded(obj: Any, *, reference: str, where: str) -> type[Instrumentation]:
    if (
        not isinstance(obj, type)
        or not issubclass(obj, Instrumentation)
        or obj is Instrumentation
    ):
        raise ConfigError(
            f"{where}: {reference!r} resolved to {obj!r}, which is not an"
            f" Instrumentation subclass"
        )

    if obj.apply is Instrumentation.apply:
        raise ConfigError(
            f"{where}: {reference!r} resolved to {obj.__qualname__}, which"
            f" does not define apply(name, module)"
        )

    return obj


def _load_safely(
    load: Callable[[], Any], *, reference: str, where: str
) -> tuple[type[Instrumentation], tuple[str, ...]]:
    # Import safety: loading the class must not import its own target.
    # Snapshot sys.modules around the load and warn loudly if any of
    # the class's triggers (or the target itself) appeared, since the
    # package has then defeated patch-before-import for exactly the
    # modules it claims. The offending names are returned as well, for
    # the listing tool to show as data.

    before = set(sys.modules)

    try:
        obj = load()
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"{where}: loading {reference!r} failed: {exc}") from exc

    cls = _check_loaded(obj, reference=reference, where=where)

    appeared = set(sys.modules) - before
    claimed = {cls.target, *_trigger_names(cls)}
    imported = sorted(appeared & claimed)
    if imported:
        warnings.warn(
            f"{where}: loading {reference!r} imported {imported}, which the"
            f" instrumentation itself claims as its target or triggers; the"
            f" module defining the class should import only wrapture, with"
            f" anything touching the target imported inside apply() and"
            f" remove(), or the patches land after the import they were"
            f" meant to precede",
            ConfigWarning,
            stacklevel=4,
        )

    return cls, tuple(imported)


def _trigger_names(cls: type[Instrumentation]) -> tuple[str, ...]:
    modules = cls.modules
    if isinstance(modules, Mapping):
        return tuple(modules)
    return tuple(modules)


def _trigger_specifiers(cls: type[Instrumentation]) -> dict[str, str | None]:
    modules = cls.modules
    if isinstance(modules, Mapping):
        return dict(modules)
    return dict.fromkeys(modules)


def _resolve_reference(reference: str, *, where: str) -> _Resolved:
    # The module:attr form: operator code next to the config file, or a
    # package under test. The class's identity is the local one.

    module_name, sep, attr_path = reference.partition(":")
    if not sep or not module_name or not attr_path:
        raise ConfigError(
            f"{where}: {reference!r} must name a module and an attribute as module:attr"
        )

    def load() -> Any:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ConfigError(
                f"{where}: cannot import module {module_name!r} named by"
                f" {reference!r}: {exc}"
            ) from exc

        resolved: Any = module
        for part in attr_path.split("."):
            try:
                resolved = getattr(resolved, part)
            except AttributeError:
                raise ConfigError(
                    f"{where}: module {module_name!r} has no attribute"
                    f" {attr_path!r} named by {reference!r}"
                ) from None
        return resolved

    cls, imported = _load_safely(load, reference=reference, where=where)
    return _resolve_class(cls, reference=reference, imported=imported)


def _resolve_class(
    cls: type[Instrumentation], *, reference: str = "", imported: tuple[str, ...] = ()
) -> _Resolved:
    # A class handed over directly, or loaded by reference: local
    # identity, no distribution.

    _check_loaded(cls, reference=reference or cls.__qualname__, where="instrumentation")

    return _Resolved(
        cls=cls,
        name=cls.name or cls.target,
        description=cls.description or _summary(cls),
        version=cls.version,
        distribution=None,
        reference=reference or f"{cls.__module__}:{cls.__qualname__}",
        imported=imported,
    )


def _resolve_registered(spec: str, *, where: str) -> _Resolved:
    # A bare or qualified entry point name. Bare resolves when exactly
    # one installed distribution registers it; the qualifier after @
    # is the distribution name, matched after PEP 503 normalisation.

    bare, sep, qualifier = spec.partition("@")
    if not bare or (sep and not qualifier):
        raise ConfigError(
            f"{where}: {spec!r} is not a valid instrumentation name; write the"
            f" entry point name, name@distribution, or a module:attr reference"
        )

    points = [point for point in _registered() if point.name == bare]

    if qualifier:
        wanted = canonicalize_name(qualifier)
        matching = [
            point
            for point in points
            if (dist := _distribution_name(point)) is not None
            and canonicalize_name(dist) == wanted
        ]
        if not matching:
            providers = sorted({d for p in points if (d := _distribution_name(p))})
            hint = (
                f"; it is registered by {providers}"
                if providers
                else "; no installed distribution registers that name"
            )
            raise ConfigError(
                f"{where}: no instrumentation named {bare!r} is registered by"
                f" distribution {qualifier!r}{hint}"
            )
        points = matching

    if not points:
        names = sorted({point.name for point in _registered()})
        hint = (
            f"; the registered names are {names}"
            if names
            else f"; no installed distribution registers any {GROUP!r} entry points"
        )
        raise ConfigError(
            f"{where}: no instrumentation named {bare!r} is registered in the"
            f" {GROUP!r} entry point group{hint}"
        )

    if len(points) > 1:
        qualified = sorted(
            f"{bare}@{d}" for p in points if (d := _distribution_name(p))
        )
        raise ConfigError(
            f"{where}: {bare!r} is registered by more than one installed"
            f" distribution; qualify it as one of {qualified}"
        )

    (point,) = points
    return _resolve_point(point, where=where)


def _resolve_point(point: metadata.EntryPoint, *, where: str) -> _Resolved:
    # Load one entry point's class and give it the entry point's
    # identity: the name, and the distribution's version and summary
    # where the class leaves its own empty.

    reference = point.value
    cls, imported = _load_safely(point.load, reference=reference, where=where)

    dist = getattr(point, "dist", None)
    distribution = _distribution_name(point)
    dist_version = str(getattr(dist, "version", "") or "") if dist is not None else ""
    summary = ""
    if dist is not None:
        try:
            summary = str(dist.metadata.get("Summary", "") or "")
        except Exception:
            summary = ""

    return _Resolved(
        cls=cls,
        name=point.name,
        description=cls.description or summary or _summary(cls),
        version=cls.version or dist_version,
        distribution=distribution,
        reference=reference,
        imported=imported,
    )


def _resolve(spec: str | type[Instrumentation], *, where: str) -> _Resolved:
    # The one door for every way of naming an instrumentation.

    if isinstance(spec, type):
        return _resolve_class(spec)

    if not isinstance(spec, str) or not spec:
        raise ConfigError(
            f"{where}: name must be an entry point name, name@distribution,"
            f" a module:attr reference, or an Instrumentation subclass, got"
            f" {spec!r}"
        )

    if ":" in spec:
        return _resolve_reference(spec, where=where)

    return _resolve_registered(spec, where=where)


# ---------------------------------------------------------------------------
# versions: the target's installed version and what it gates
# ---------------------------------------------------------------------------

# packages_distributions() scans every installed distribution, so the
# mapping is cached against the sys.path it was computed for: a test
# that prepends a directory, or a config's pythonpath, invalidates it
# naturally.

_distributions_cache: tuple[tuple[str, ...], Mapping[str, list[str]]] | None = None
_distributions_lock = threading.Lock()


def _packages_distributions() -> Mapping[str, list[str]]:
    global _distributions_cache

    path = tuple(sys.path)
    with _distributions_lock:
        cached = _distributions_cache
        if cached is not None and cached[0] == path:
            return cached[1]

        mapping = metadata.packages_distributions()
        _distributions_cache = (path, mapping)
        return mapping


def _target_version(target: str) -> str | None:
    """The installed version of the distribution behind a top-level
    import name, from metadata alone, or None when no distribution
    stands behind it."""

    if not target:
        return None

    candidates = list(_packages_distributions().get(target, ()))
    candidates.append(target)

    for candidate in candidates:
        try:
            return metadata.version(candidate)
        except metadata.PackageNotFoundError:
            continue

    return None


@dataclass(frozen=True)
class _TriggerPlan:
    # One trigger's apply-time verdict: registers, or skipped for the
    # reason given.

    name: str
    specifier: str | None
    skipped: str | None = None


def _satisfies(specifier: str, version: str | None) -> bool | None:
    # True or False against a parseable version, None when the version
    # is unknown or unparseable.

    if version is None:
        return None

    try:
        return SpecifierSet(specifier).contains(Version(version), prereleases=True)
    except (InvalidVersion, InvalidSpecifier):
        return None


def _plan(cls: type[Instrumentation], version: str | None) -> list[_TriggerPlan]:
    """What applying the class against the given target version would
    register: every trigger with the reason it is skipped, if it is.
    The dry run the listing tool shows and apply() follows."""

    plans: list[_TriggerPlan] = []
    found = version if version is not None else "unknown"

    overall: str | None = None
    if cls.supports:
        verdict = _satisfies(cls.supports, version)
        if verdict is None:
            overall = (
                f"target version unknown, so supports {cls.supports!r} cannot be"
                f" checked"
            )
        elif not verdict:
            overall = f"target {found} is outside supports {cls.supports!r}"

    for name, specifier in _trigger_specifiers(cls).items():
        skipped = overall
        if skipped is None and specifier is not None:
            verdict = _satisfies(specifier, version)
            if verdict is None:
                skipped = f"target version unknown, so {specifier!r} cannot be checked"
            elif not verdict:
                skipped = f"target {found} is outside {specifier!r}"
        plans.append(_TriggerPlan(name, specifier, skipped))

    return plans


# ---------------------------------------------------------------------------
# config entries and the checks between them
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentEntry:
    """One [[instrument]] entry: which instrumentation to apply, and
    with what settings.

    `name` is a registered entry point name (`"flask"`), a qualified
    one when two distributions register the same name
    (`"requests@wrapture-instrumentation-contrib"`), a module:attr
    reference to an unregistered class (operator code next to the
    config file, or a package under test), or, from code, the class
    itself. `settings` are the values for the instrumentation's
    declared settings, the entry's extra keys in the file; an unknown
    name or a value of the wrong outer type is a ConfigError when the
    config is built. `enabled = False` keeps the entry but applies
    nothing: still validated, but taking part in no conflict check and
    satisfying no requirement, and listed as disabled by report().
    """

    name: str | type[Instrumentation]
    enabled: bool = True
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.name, type):
            if not issubclass(self.name, Instrumentation):
                raise ConfigError(
                    f"instrument entry: name must be an instrumentation name, a"
                    f" module:attr reference or an Instrumentation subclass,"
                    f" got {self.name!r}"
                )
        elif not isinstance(self.name, str) or not self.name:
            raise ConfigError(
                f"instrument entry: name must be a non-empty string, got {self.name!r}"
            )

        if not isinstance(self.enabled, bool):
            raise ConfigError(
                f"instrument entry {self.label!r}: enabled must be true or false,"
                f" got {self.enabled!r}"
            )

        if not isinstance(self.settings, Mapping) or not all(
            isinstance(key, str) for key in self.settings
        ):
            raise ConfigError(
                f"instrument entry {self.label!r}: settings must be a mapping"
                f" with string keys, got {self.settings!r}"
            )

        object.__setattr__(self, "settings", dict(self.settings))

    @property
    def label(self) -> str:
        """How the entry names its instrumentation, for messages."""

        if isinstance(self.name, type):
            return f"{self.name.__module__}:{self.name.__qualname__}"
        return self.name


@dataclass(frozen=True)
class _Planned:
    # An entry resolved at config build: the class, its identity, and
    # the settings it will be constructed with.

    entry: InstrumentEntry
    resolved: _Resolved

    @property
    def enabled(self) -> bool:
        return self.entry.enabled


def _plan_entries(entries: Sequence[InstrumentEntry]) -> tuple[_Planned, ...]:
    """Resolve and validate a config's [[instrument]] entries: every
    class loads and takes the entry's settings, and the enabled entries
    neither conflict nor lack a required target among themselves."""

    planned: list[_Planned] = []
    for entry in entries:
        if not isinstance(entry, InstrumentEntry):
            raise ConfigError(
                f"instrument entries must be InstrumentEntry instances, got {entry!r}"
            )

        where = f"instrument entry {entry.label!r}"
        resolved = _resolve(entry.name, where=where)
        _resolve_settings(resolved.cls, entry.settings, where)
        planned.append(_Planned(entry, resolved))

    _check_between(planned)
    return tuple(planned)


def _check_between(planned: Sequence[_Planned]) -> None:
    # The conflict check (two for one target) and the requires check,
    # among the enabled entries of one config. Because every trigger
    # lives under its class's target, two classes can only claim one
    # trigger module by sharing a target, so the target check is also
    # the double-patch check.

    by_target: dict[str, _Planned] = {}

    for item in planned:
        if not item.enabled:
            continue

        cls = item.resolved.cls
        other = by_target.get(cls.target)
        if other is not None:
            raise ConfigError(
                f"instrument entries {other.entry.label!r} and"
                f" {item.entry.label!r} both instrument target {cls.target!r};"
                f" enable one, or disable one with enabled = false"
            )
        by_target[cls.target] = item

    for item in planned:
        if not item.enabled:
            continue

        for required in item.resolved.cls.requires:
            if required not in by_target:
                raise ConfigError(
                    f"instrument entry {item.entry.label!r} requires an active"
                    f" instrumentation for target {required!r}, and none is"
                    f" enabled in this config; requirements are not pulled in"
                    f" automatically, so add an [[instrument]] entry for it"
                )


# ---------------------------------------------------------------------------
# the live registry: one trampoline per trigger, one instrumentation per
# target, process-wide
# ---------------------------------------------------------------------------

_registry_lock = threading.RLock()
_active_targets: dict[str, Instrumentation] = {}
_trampolines: dict[str, _Trampoline] = {}


class _Claim:
    # One instrumentation's interest in one trigger, for one record.

    def __init__(self, instance: Instrumentation, record: AppliedConfig) -> None:
        self.instance = instance
        self.record = record
        self.released = False

    def fire(self, module: Any) -> None:
        with _registry_lock:
            if self.released:
                return

        record = self.record
        with record._lock:
            if record._reverted:
                return

        name = getattr(module, "__name__", "?")
        try:
            self.instance._fire(name, module)
        except Exception as exc:
            # Loud during apply, warn-and-continue from inside an
            # application import: observation must never fail the
            # import it rode in on.

            if record._applying:
                raise

            warnings.warn(
                f"instrumentation {self.instance.name!r} failed to apply to"
                f" {name!r} when it was imported: {exc!r}. The import"
                f" continues; nothing is applied for that module.",
                ConfigWarning,
                stacklevel=2,
            )


class _Trampoline:
    # The one wrapt post-import hook per trigger module, routed to
    # whichever claims are live when it fires. Repeated enter and exit
    # in a test suite therefore never accumulates neutralised hooks.

    def __init__(self, name: str) -> None:
        self.name = name
        self.claims: list[_Claim] = []
        self.armed = False

    def __call__(self, module: Any) -> None:
        with _registry_lock:
            claims = list(self.claims)
            self.claims.clear()
            self.armed = False

        for claim in claims:
            claim.fire(module)


def _claim(name: str, claim: _Claim) -> None:
    # Fire now for a module already imported, the way wrapt would;
    # otherwise queue on the trampoline, registering it with wrapt the
    # first time (or again after it has fired). The re-check covers the
    # import landing between the first look and the queueing.

    module = sys.modules.get(name)
    if module is not None:
        claim.fire(module)
        return

    with _registry_lock:
        trampoline = _trampolines.setdefault(name, _Trampoline(name))
        trampoline.claims.append(claim)
        arm = not trampoline.armed
        if arm:
            trampoline.armed = True

    if arm:
        wrapt.register_post_import_hook(trampoline, name)
        return

    module = sys.modules.get(name)
    if module is not None:
        with _registry_lock:
            stranded = claim in trampoline.claims
            if stranded:
                trampoline.claims.remove(claim)

        if stranded:
            claim.fire(module)


def _release(claims: Iterable[_Claim]) -> None:
    # Neutralise claims that have not fired: they leave the trampoline
    # queue, and one that is mid-flight sees the flag.

    with _registry_lock:
        for claim in claims:
            claim.released = True
            for trampoline in _trampolines.values():
                if claim in trampoline.claims:
                    trampoline.claims.remove(claim)


def _activate(instances: Sequence[Instrumentation]) -> None:
    # Check the batch against everything live, then register it
    # atomically, so two concurrent applies cannot both win a target.
    # One instrumentation per target is also one per trigger module,
    # since every trigger lives under its target.

    with _registry_lock:
        for instance in instances:
            target = type(instance).target
            holder = _active_targets.get(target)
            if holder is not None:
                raise ConfigError(
                    f"instrumentation {instance.name!r} cannot be applied:"
                    f" target {target!r} already has {holder.name!r} active"
                    f" from another applied config; revert that first"
                )

        for instance in instances:
            _active_targets[type(instance).target] = instance


def _deactivate(instance: Instrumentation) -> None:
    with _registry_lock:
        if _active_targets.get(type(instance).target) is instance:
            del _active_targets[type(instance).target]


def _active() -> dict[str, Instrumentation]:
    """The live instrumentations, by target."""

    with _registry_lock:
        return dict(_active_targets)


# ---------------------------------------------------------------------------
# applying and removing, on behalf of a config record
# ---------------------------------------------------------------------------


class _Applied:
    # What one record holds per applied instrumentation: the instance
    # and the claims to release on revert.

    def __init__(self, instance: Instrumentation, claims: list[_Claim]) -> None:
        self.instance = instance
        self.claims = claims


def _apply_planned(planned: Sequence[_Planned], record: AppliedConfig) -> None:
    """Apply a config's resolved entries on behalf of its record:
    construct each enabled instrumentation, register the batch, then
    configure each and claim its triggers, warning about any a version
    gate skips. Errors propagate; the caller unwinds the record."""

    instances: list[Instrumentation] = []
    for item in planned:
        if not item.enabled:
            continue

        instance = item.resolved.cls(**item.entry.settings)
        instance._identify(item.resolved)
        instances.append(instance)

    _activate(instances)

    # From here the instances are live in the registry, so each goes on
    # the record before anything else can fail, and revert takes it
    # down again.

    for instance in instances:
        holder = _Applied(instance, [])
        with record._lock:
            record._instrumentations.append(holder)

        instance.configure()

        plans = _plan(type(instance), instance.target_version)
        skipped = [plan for plan in plans if plan.skipped is not None]
        if skipped:
            details = "; ".join(f"{plan.name}: {plan.skipped}" for plan in skipped)
            warnings.warn(
                f"instrumentation {instance.name!r} (version"
                f" {instance.version or 'local'}) skips"
                f" {len(skipped)} of {len(plans)} trigger modules: {details}",
                ConfigWarning,
                stacklevel=4,
            )

        registered = [plan.name for plan in plans if plan.skipped is None]
        instance._triggers = tuple(registered)

        for trigger in registered:
            claim = _Claim(instance, record)
            with _registry_lock:
                holder.claims.append(claim)
            _claim(trigger, claim)


def _revert_applied(applied: Sequence[_Applied]) -> None:
    """Take down a record's instrumentations, most recent first:
    release unfired claims, free the registry, run the teardown, and
    warn about any that declared itself not removable."""

    for holder in reversed(applied):
        instance = holder.instance

        _release(holder.claims)
        _deactivate(instance)

        if not type(instance).removable:
            warnings.warn(
                f"instrumentation {instance.name!r} declares itself not"
                f" removable; its undo callbacks run, but whatever else it"
                f" patched may remain in place",
                ConfigWarning,
                stacklevel=3,
            )

        instance._teardown()


# ---------------------------------------------------------------------------
# the context manager
# ---------------------------------------------------------------------------


class Instrumented:
    """The scope instrumentation() returns: applies its instrumentations
    on entry and removes them on exit, yielding the AppliedConfig
    record."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._record: AppliedConfig | None = None

    def __enter__(self) -> AppliedConfig:
        if self._record is not None:
            raise RuntimeError("instrumentation scope is already active")

        self._record = self._config.apply()
        return self._record

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        record = self._record
        self._record = None

        if record is not None:
            record.revert()


def instrumentation(
    *items: Any, allow_unremovable: bool = False, **settings: Any
) -> Instrumented:
    """Apply one or more instrumentations for the duration of a block.

    Each item is an instrumentation name (bare, qualified as
    `name@distribution`, or a module:attr reference), an Instrumentation
    subclass, or an `(item, settings)` pair; with exactly one item its
    settings may be passed as keyword arguments instead. Entry refuses,
    before patching anything, an instrumentation declared not removable
    (pass `allow_unremovable=True` to override) and one whose target
    another applied config already instruments. Triggers already
    imported apply on entry, the rest when their module arrives; exit
    removes everything in reverse and neutralises what never fired.
    The block yields the AppliedConfig record, so pairs with timeline()
    and window() as any other config does.
    """

    from .config import Config

    entries: list[InstrumentEntry] = []
    for item in items:
        if isinstance(item, tuple):
            if len(item) != 2:
                raise ConfigError(
                    f"instrumentation(): an item pair is (name, settings), got {item!r}"
                )
            spec, given = item
            entries.append(InstrumentEntry(spec, settings=given))
        else:
            entries.append(InstrumentEntry(item))

    if settings:
        if len(entries) != 1:
            raise ConfigError(
                "instrumentation(): keyword settings apply to exactly one item;"
                " with several, pass (name, settings) pairs"
            )
        (only,) = entries
        entries = [InstrumentEntry(only.name, settings={**only.settings, **settings})]

    if not entries:
        raise ConfigError("instrumentation() needs at least one instrumentation")

    config = Config(instrument=entries)

    if not allow_unremovable:
        for item in config._instrument_planned:
            if not item.resolved.cls.removable:
                raise ConfigError(
                    f"instrumentation {item.resolved.name!r} declares itself not"
                    f" removable, so it cannot be scoped to a block; pass"
                    f" allow_unremovable=True to apply it anyway"
                )

    return Instrumented(config)
