"""Instrumentation packages: a self-describing contract for code that
patches one target package on wrapture's behalf.

A package (or a module next to a config file) ships an Instrumentation
subclass: class data says what it is and covers, and methods decorated
with @instrumentation_hook patch one trigger module each once that
module is imported, registering their undo with on_cleanup(). wrapture
discovers subclasses through the `wrapture.instrumentation` entry
point group or by module:attr reference, validates them, applies them
from an [[instrument]] config entry or the instrumentation() context
manager, reports on them, and removes them on revert, without knowing
where the hook code lives or importing it ahead of the target. The
base class owns apply(name, module) and remove(name, module), so
wrapture's dispatch and a package's own tests drive the same methods.

The module that defines a subclass must not import the target it
patches; loading the class would then defeat patch-before-import for
the very modules it claims. Hook code lives in a sibling module that
imports only wrapture at its top level and receives the trigger module
as a parameter, importing anything more of the target inside the
functions that need it.
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
from typing import TYPE_CHECKING, Any, Protocol, cast

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


class InstrumentationHookMethod(Protocol):
    """What @instrumentation_hook returns: the method itself, still
    callable and usable as the class's method, carrying the `cleanup`
    decorator that pairs a per-trigger teardown method with it."""

    def __call__(self, instance: Any, name: str, module: Any) -> Any: ...

    cleanup: Callable[[Callable[..., Any]], Callable[..., Any]]


@dataclass(frozen=True)
class _HookDeclaration:
    # One trigger module declared on a hook method: the name, and the
    # per-trigger overrides riding on the decorator.

    module: str
    supports: str | None
    removable: bool | None


@dataclass(frozen=True)
class _Hook:
    # One trigger's resolved handler on a class: the method (as the
    # plain function, called with the instance explicitly), the
    # declaration it came from, and the paired cleanup method if one
    # was declared.

    attribute: str
    fn: Callable[..., Any]
    declaration: _HookDeclaration
    cleanup: Callable[..., Any] | None

    @property
    def module(self) -> str:
        return self.declaration.module

    @property
    def supports(self) -> str | None:
        return self.declaration.supports

    @property
    def removable(self) -> bool | None:
        return self.declaration.removable


def instrumentation_hook(
    module: str, *, supports: str | None = None, removable: bool | None = None
) -> Callable[[Callable[..., Any]], InstrumentationHookMethod]:
    """Mark a method of an Instrumentation subclass as the hook for one
    trigger module.

    The decorated method is called as `method(self, name, module)` when
    the named module is imported (immediately if it already was), with
    the trigger's name and the module object. The trigger string
    appears only here; the class's trigger set is derived from its
    decorated methods, and the method name itself is free. Stack the
    decorator to serve several triggers with one method, which is why
    the signature always takes `name`.

    `supports` is a PEP 440 specifier gating this one trigger on the
    target's installed version, for a module that only exists from some
    version on; the class's own `supports` is the target-level range.
    `removable` overrides the class's `removable` claim for this one
    trigger; the class-level claim consumers see is true only when
    every trigger in play is removable.

    The returned method carries a `cleanup` decorator pairing a
    per-trigger teardown method with it, for undo that does not
    decompose into on_cleanup() callbacks:

        @wrapture.instrumentation_hook("flask.app")
        def flask_app(self, name, module): ...

        @flask_app.cleanup
        def remove_flask_app(self, name, module): ...

    On removal the on_cleanup() callbacks registered during the hook
    run first, most recent first, then the paired cleanup method.
    """

    if not isinstance(module, str) or not module:
        raise ConfigError(
            f"instrumentation_hook: module must be a trigger module name,"
            f" got {module!r}"
        )
    if supports is not None:
        _check_specifier(supports, f"instrumentation_hook({module!r}): supports")
    if removable is not None and not isinstance(removable, bool):
        raise ConfigError(
            f"instrumentation_hook({module!r}): removable must be True or"
            f" False, got {removable!r}"
        )

    def mark(fn: Callable[..., Any]) -> InstrumentationHookMethod:
        if not callable(fn):
            raise ConfigError(
                f"instrumentation_hook({module!r}) decorates a method, got {fn!r}"
            )

        declarations: list[_HookDeclaration] | None = getattr(
            fn, "_wrapture_hook_triggers", None
        )

        # First decoration: attach the declaration list and the cleanup
        # pairing decorator; a stacked decoration appends to the list.

        if declarations is None:
            declarations = []
            fn._wrapture_hook_triggers = declarations  # type: ignore[attr-defined]
            fn._wrapture_hook_cleanup = None  # type: ignore[attr-defined]

            def pair(cleanup_fn: Callable[..., Any]) -> Callable[..., Any]:
                if not callable(cleanup_fn):
                    raise ConfigError(
                        f"@{fn.__name__}.cleanup decorates a method, got {cleanup_fn!r}"
                    )
                if fn._wrapture_hook_cleanup is not None:  # type: ignore[attr-defined]
                    raise ConfigError(
                        f"hook method {fn.__name__!r} already has a cleanup"
                        f" method paired with it"
                    )
                fn._wrapture_hook_cleanup = cleanup_fn  # type: ignore[attr-defined]
                return cleanup_fn

            fn.cleanup = pair  # type: ignore[attr-defined]

        if any(declared.module == module for declared in declarations):
            raise ConfigError(
                f"hook method {fn.__name__!r} declares trigger module {module!r} twice"
            )

        declarations.append(_HookDeclaration(module, supports, removable))
        return cast(InstrumentationHookMethod, fn)

    return mark


class Instrumentation:
    """Instrumentation for one target package, shipped as a subclass.

    Class data declares what the instrumentation is and covers: the
    `target` import name, the version range it `supports`, other
    targets it `requires`, whether it is `removable`, and the
    `settings` it takes. The trigger modules are declared by methods
    decorated with `@wrapture.instrumentation_hook`, one hook per
    trigger, and the base class owns `apply(name, module)` and
    `remove(name, module)`: wrapture's dispatch and a package's own
    tests call the same methods, so the direct testing recipe and the
    deferred import path behave identically. The module defining a
    subclass must not import the target; the hooks module beside it
    imports only wrapture at its top level, and anything more of the
    target is imported inside the hook functions that need it.
    """

    # -- class data the subclass declares ------------------------------

    # Identity. Each defaults when left empty: name from the entry point
    # name (or the target, for a class named by reference), description
    # from the distribution's summary (or the class docstring's first
    # line), version from the distribution (or none, for a local class).
    # A class in a multi-target distribution should set description,
    # since the distribution's summary describes the whole collection.

    name: str = ""
    description: str = ""
    version: str = ""

    # Coverage. target is exactly one top-level import name and every
    # declared trigger module must live under it; supports is a PEP 440
    # specifier against the target's installed version (per-trigger
    # ranges ride on the instrumentation_hook decorator); requires
    # names other targets that must have an active instrumentation in
    # the same config, a single name or a sequence of them.

    target: str = ""
    supports: str = ""
    requires: str | Sequence[str] = ()

    # The claim report() and revert() trust: a package must say it can
    # undo itself. This is the default for every trigger; a hook's
    # removable= overrides it per trigger, and the class-level claim
    # consumers see is true only when every trigger in play is.
    # Callbacks registered with on_cleanup() run either way.

    removable: bool = False

    # The declaration, name to Setting. The resolved values take the
    # same name on the instance, an ordinary attribute __init__ assigns
    # that shadows this one for instance access.

    settings: Mapping[str, Any] = {}

    # The per-class hook table, trigger module name to _Hook, built by
    # _check_class from the decorated methods when the class is defined.

    _hooks: Mapping[str, _Hook] = {}

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
        self._triggers: tuple[str, ...] = tuple(type(self)._hooks)
        self._target_version = _target_version(cls.target)

    # -- the optional one-time hook the subclass overrides --------------

    def configure(self) -> None:
        """Optional one-time work after construction and before any
        trigger fires: validate settings beyond their outer type (raise
        ConfigError, which surfaces at config time), register a sink,
        prepare state. The default does nothing."""

    # -- applying and removing: the same doors for wrapture and tests ---

    def apply(self, name: str, module: Any) -> None:
        """Apply one trigger: run its hook method with the trigger set
        for this thread, so on_cleanup() files undo under it, and
        record the trigger as applied.

        Called by wrapture once per trigger when its module is imported
        (immediately if it already was), and directly by a package's
        own tests, with identical behaviour. A name the class does not
        declare, or a trigger already applied, raises ConfigError. A
        hook that raises has the callbacks it registered run at once,
        so its partial work does not linger, and the error propagates.
        """

        hook = type(self)._hooks.get(name)
        if hook is None:
            declared = sorted(type(self)._hooks)
            raise ConfigError(
                f"instrumentation {self.name!r}: {name!r} is not a declared"
                f" trigger module; the declared triggers are {declared}"
            )

        with self._lock:
            if name in self._applied:
                raise ConfigError(
                    f"instrumentation {self.name!r}: trigger {name!r} is"
                    f" already applied"
                )

        self._local.trigger = name
        try:
            hook.fn(self, name, module)
        except BaseException:
            self._local.trigger = None
            _run_callbacks(self._take_callbacks(name), self, name)
            raise
        finally:
            self._local.trigger = None

        with self._lock:
            self._applied.append(name)
            self._modules[name] = module

    def remove(self, name: str, module: Any) -> None:
        """Undo one trigger: forget it as applied, run the on_cleanup()
        callbacks registered during its hook (most recent first), then
        its paired cleanup method if the hook declared one.

        Called for each applied trigger, in reverse order, on revert,
        and directly by tests. A trigger that is not applied is a
        no-op. A callback or cleanup method that raises is warned
        about and the removal continues.
        """

        with self._lock:
            if name not in self._applied:
                return
            self._applied.remove(name)
            self._modules.pop(name, None)

        _run_callbacks(self._take_callbacks(name), self, name)

        hook = type(self)._hooks.get(name)
        if hook is not None and hook.cleanup is not None:
            try:
                hook.cleanup(self, name, module)
            except Exception as exc:
                warnings.warn(
                    f"instrumentation {self.name!r}: cleanup method for"
                    f" {name!r} raised {exc!r}; continuing",
                    ConfigWarning,
                    stacklevel=2,
                )

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
        """The trigger module whose hook is running on this thread, so
        on_cleanup() can tag callbacks without being told; None outside
        a hook. Inside a hook the `name` parameter is the same value.
        """

        value: str | None = getattr(self._local, "trigger", None)
        return value

    def on_cleanup(self, callback: Callable[[], Any]) -> None:
        """Register an undo callback against the trigger currently
        applying, or against the whole instrumentation when called
        outside a hook (from configure(), say).

        Call it as many times as there are things to undo: every
        callback registered for a trigger runs when that trigger is
        removed, most recent first, continuing past one that raises;
        its return value is ignored, so a binding's remove() or a
        group's passes straight in. No dedupe, the same callable twice
        runs twice. Thread-safe.
        """

        if not callable(callback):
            raise TypeError(f"on_cleanup() takes a callable, got {callback!r}")

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

    def _removable_over(self, names: Iterable[str] | None = None) -> bool:
        # The effective removability claim over a set of triggers, each
        # hook's removable= over the class default; over the registered
        # triggers when none are given, and the bare class claim when
        # the set is empty.

        cls = type(self)
        chosen = tuple(names) if names is not None else self._triggers
        if not chosen:
            return cls.removable

        return all(
            hook.removable if hook.removable is not None else cls.removable
            for name in chosen
            if (hook := cls._hooks.get(name)) is not None
        )

    def _unremovable_triggers(self) -> tuple[str, ...]:
        # The registered triggers whose effective claim is False, for
        # the revert warning to name.

        cls = type(self)
        return tuple(
            name
            for name in self._triggers
            if (hook := cls._hooks.get(name)) is not None
            and not (hook.removable if hook.removable is not None else cls.removable)
        )

    def _teardown(self) -> None:
        # Call remove() per applied trigger, most recent first, then
        # the whole-instrumentation callbacks; snapshot under the lock,
        # run outside it. A remove() that raises is warned about and
        # the teardown continues.

        with self._lock:
            names = list(reversed(self._applied))
            modules = {name: self._modules[name] for name in names}

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
            self._applied.clear()
            self._modules.clear()
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

        if self._removable_over():
            parts.append("removable")
        else:
            partial = self._unremovable_triggers()
            if partial and len(partial) < len(self._triggers):
                parts.append(f"not removable ({', '.join(partial)})")
            else:
                parts.append("not removable")
        parts.append(f"{self._callback_count()} cleanup callbacks")

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
                f"instrumentation {instance.name!r}: cleanup callback"
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

    # A subclass carrying the old contract's class data is refused with
    # a pointer at the new shape rather than silently ignored.

    if "modules" in vars(cls):
        raise ConfigError(
            f"{where}: modules is no longer class data; declare each trigger"
            f" with an @instrumentation_hook method instead"
        )

    # Triggers: collect the decorated hook methods, the subclass's own
    # first and then inherited ones, each attribute name resolved once
    # along the MRO so a subclass overriding a hook method replaces it.

    hooks: dict[str, _Hook] = {}
    seen_attributes: set[str] = set()

    for klass in cls.__mro__:
        if klass in (Instrumentation, object):
            continue

        for attribute, value in vars(klass).items():
            if attribute in seen_attributes:
                continue
            seen_attributes.add(attribute)

            declarations = getattr(value, "_wrapture_hook_triggers", None)
            if not declarations:
                continue

            # A hook method must not shadow the base class surface: a
            # method named apply would replace the driver it needs.

            if hasattr(Instrumentation, attribute):
                raise ConfigError(
                    f"{where}: hook method {attribute!r} shadows an attribute"
                    f" of the Instrumentation base class; rename the method"
                )

            cleanup = getattr(value, "_wrapture_hook_cleanup", None)

            # Stacked decorators apply bottom-up, so the recorded order
            # is reversed to read top-down as the source does.

            for declaration in reversed(declarations):
                name = declaration.module
                if name != target and not name.startswith(f"{target}."):
                    raise ConfigError(
                        f"{where}: trigger module {name!r} is not under target"
                        f" {target!r}; an instrumentation covers one target"
                        f" and every trigger must live under it"
                    )

                other = hooks.get(name)
                if other is not None:
                    raise ConfigError(
                        f"{where}: hook methods {other.attribute!r} and"
                        f" {attribute!r} both declare trigger module {name!r}"
                    )

                hooks[name] = _Hook(attribute, value, declaration, cleanup)

    cls._hooks = hooks

    if cls.supports:
        _check_specifier(cls.supports, f"{where}: supports")

    # requires: a single target name or a sequence of them, normalised
    # to a tuple so a bare string means one name and is never iterated
    # character by character.

    requires = cls.requires
    normalised: tuple[str, ...]
    if isinstance(requires, str):
        normalised = (requires,) if requires else ()
    elif isinstance(requires, Sequence):
        normalised = tuple(requires)
    else:
        raise ConfigError(
            f"{where}: requires must be a target name or a sequence of them"
        )
    for required in normalised:
        if not isinstance(required, str) or not required:
            raise ConfigError(
                f"{where}: requires must be target names, got {required!r}"
            )
    cls.requires = normalised

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

    if not obj._hooks:
        raise ConfigError(
            f"{where}: {reference!r} resolved to {obj.__qualname__}, which"
            f" declares no trigger modules; mark one or more methods with"
            f" @wrapture.instrumentation_hook"
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
            f" module defining the class must not import the target, and its"
            f" hooks module imports only wrapture at top level, or the"
            f" patches land after the import they were meant to precede",
            ConfigWarning,
            stacklevel=4,
        )

    return cls, tuple(imported)


def _trigger_names(cls: type[Instrumentation]) -> tuple[str, ...]:
    return tuple(cls._hooks)


def _trigger_specifiers(cls: type[Instrumentation]) -> dict[str, str | None]:
    return {name: hook.supports for name, hook in cls._hooks.items()}


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
    config is built. `triggers` optionally names a subset of the
    class's declared trigger modules to register, a single name or a
    sequence of them, for testing hooks in isolation or excluding one
    hook of a large instrumentation; a name the class does not declare
    is a ConfigError. `enabled = False` keeps the entry but applies
    nothing: still validated, but taking part in no conflict check and
    satisfying no requirement, and listed as disabled by report().
    """

    name: str | type[Instrumentation]
    enabled: bool = True
    settings: Mapping[str, Any] = field(default_factory=dict)
    triggers: str | Sequence[str] = ()

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

        # triggers: a single name or a sequence, normalised to a tuple,
        # a bare string meaning one name; membership in the class's
        # declared set is checked when the config resolves the class.

        triggers = self.triggers
        normalised: tuple[str, ...]
        if isinstance(triggers, str):
            normalised = (triggers,) if triggers else ()
        elif isinstance(triggers, Sequence):
            normalised = tuple(triggers)
        else:
            raise ConfigError(
                f"instrument entry {self.label!r}: triggers must be a trigger"
                f" module name or a sequence of them, got {triggers!r}"
            )
        for trigger in normalised:
            if not isinstance(trigger, str) or not trigger:
                raise ConfigError(
                    f"instrument entry {self.label!r}: triggers must be trigger"
                    f" module names, got {trigger!r}"
                )
        object.__setattr__(self, "triggers", normalised)

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

        declared = set(resolved.cls._hooks)
        unknown = sorted(set(entry.triggers) - declared)
        if unknown:
            raise ConfigError(
                f"{where}: triggers {unknown} are not declared by the class;"
                f" the declared triggers are {sorted(declared)}"
            )

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
            self.instance.apply(name, module)
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

        # An entry's triggers subset excludes hooks outright: they are
        # neither registered nor warned about, unlike a version gate.

        plans = _plan(type(instance), instance.target_version)
        chosen = item.entry.triggers
        if chosen:
            plans = [plan for plan in plans if plan.name in chosen]

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

        if not instance._removable_over():
            partial = instance._unremovable_triggers()
            scope = (
                f" for triggers {', '.join(partial)}"
                if partial and len(partial) < len(instance._triggers)
                else ""
            )
            warnings.warn(
                f"instrumentation {instance.name!r} declares itself not"
                f" removable{scope}; its cleanup callbacks run, but whatever"
                f" else it patched may remain in place",
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
    *items: Any,
    allow_unremovable: bool = False,
    triggers: str | Sequence[str] | None = None,
    **settings: Any,
) -> Instrumented:
    """Apply one or more instrumentations for the duration of a block.

    Each item is an instrumentation name (bare, qualified as
    `name@distribution`, or a module:attr reference), an Instrumentation
    subclass, or an `(item, settings)` pair; with exactly one item its
    settings may be passed as keyword arguments instead. `triggers`,
    valid with exactly one item like the keyword settings, names a
    subset of the class's declared trigger modules to register, so a
    package's tests can apply one hook in isolation. Entry refuses,
    before patching anything, an instrumentation not removable over
    the triggers in play (pass `allow_unremovable=True` to override)
    and one whose target another applied config already instruments.
    Triggers already imported apply on entry, the rest when their
    module arrives; exit removes everything in reverse and neutralises
    what never fired. The block yields the AppliedConfig record, so
    pairs with timeline() and window() as any other config does.
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

    if triggers is not None:
        if len(entries) != 1:
            raise ConfigError("instrumentation(): triggers applies to exactly one item")
        (only,) = entries
        entries = [
            InstrumentEntry(only.name, settings=only.settings, triggers=triggers)
        ]

    if not entries:
        raise ConfigError("instrumentation() needs at least one instrumentation")

    config = Config(instrument=entries)

    # The removability gate is evaluated over the triggers the entry
    # will actually register: a partly removable class is scopeable to
    # a subset of its removable triggers without the escape hatch.

    if not allow_unremovable:
        for item in config._instrument_planned:
            cls = item.resolved.cls
            chosen = item.entry.triggers or tuple(cls._hooks)
            effective = all(
                hook.removable if hook.removable is not None else cls.removable
                for name in chosen
                if (hook := cls._hooks.get(name)) is not None
            )
            if chosen and not effective or not chosen and not cls.removable:
                raise ConfigError(
                    f"instrumentation {item.resolved.name!r} declares itself not"
                    f" removable over the triggers in play, so it cannot be"
                    f" scoped to a block; pass allow_unremovable=True to apply"
                    f" it anyway"
                )

    return Instrumented(config)
