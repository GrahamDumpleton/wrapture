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

import atexit
import importlib
import os
import sys
import threading
import tomllib
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

import wrapt

from .bindings import Binding, _select_members, binding
from .capture import REFERENCE, CapturePolicy, _resolve_policy
from .capture import redact as _redact
from .exceptions import ConfigError, ConfigWarning
from .sinks import (
    Depth,
    Fanout,
    Filter,
    JSONLines,
    Printer,
    Sample,
    Sink,
    add_sink,
    remove_sink,
)
from .timeline import Tape


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
    subtracting from a match. `redact` names parameters whose values
    are replaced with a marker on this entry's bindings, for secrets
    that must not reach any sink. Every such field accepts one string
    or a list; `name` and `match` are mutually exclusive and one is
    required, and `exclude` only accompanies `match`.
    """

    target: str
    name: str | Sequence[str] = ()
    match: str | Sequence[str] = ()
    exclude: str | Sequence[str] = ()
    redact: str | Sequence[str] = ()

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

        for key in ("name", "match", "exclude", "redact"):
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
    """One setup rule, in one of two forms.

    The single form names `module` (the trigger) and `call` (a
    module:attr reference): the callback runs with the module once it
    is imported, or immediately if it already was. The group form
    names `group`, an entry point group some installed package
    declares, each of whose entries maps a trigger module to a
    handler, so one entry activates a whole family of handlers. The
    two forms are mutually exclusive.

    `options` are extra keyword arguments passed to the handler along
    with the module, `handler(module, **options)`, so one handler can
    be specialised from the config; with no options the call is
    exactly `handler(module)`. In the group form every handler in the
    family receives the same options. Handler references resolve only
    when their hook fires, so naming operator code here cannot cause
    it to be imported before the module it wants to instrument.
    """

    module: str = ""
    call: str = ""
    group: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key in ("module", "call", "group"):
            if not isinstance(getattr(self, key), str):
                raise ConfigError(
                    f"setup entry: {key} must be a string, got {getattr(self, key)!r}"
                )

        if self.group:
            if self.module or self.call:
                raise ConfigError(
                    f"setup group {self.group!r}: group is an alternative"
                    f" to module and call, not a companion; use one form"
                    f" or the other"
                )
        else:
            if not self.module:
                raise ConfigError(
                    f"setup entry: module must be a non-empty module name,"
                    f" got {self.module!r}"
                )

            _split_reference(self.call, key="call", where=f"setup for {self.module!r}")

        if not isinstance(self.options, Mapping) or not all(
            isinstance(key, str) for key in self.options
        ):
            raise ConfigError(
                f"setup entry: options must be a mapping with string keys,"
                f" got {self.options!r}"
            )

        object.__setattr__(self, "options", dict(self.options))


class AppliedConfig:
    """The live record of what one Config.apply() installed.

    Observe entries defer: apply() registers a post-import hook per
    target module, which fires immediately for a module already
    imported and otherwise when the application imports it. The
    bindings recorded here therefore grow over the life of the
    process; `pending` names the entries still waiting for their
    module, `report()` renders the whole picture, and `revert()`
    takes everything down again.
    """

    def __init__(self, sink: Sink | None) -> None:
        self._lock = threading.Lock()
        self._bindings: list[Binding] = []
        self._pending: list[ObserveEntry] = []
        self._sink = sink
        self._reverted = False
        self._suspended = False
        self._applying = False

    @property
    def bindings(self) -> tuple[Binding, ...]:
        """The bindings applied so far, in application order."""

        with self._lock:
            return tuple(self._bindings)

    @property
    def sink(self) -> Sink | None:
        """The process sink the config registered, as registered: with
        sampling configured this is the wrapping Sample."""

        return self._sink

    @property
    def pending(self) -> tuple[ObserveEntry, ...]:
        """The observe entries whose target module has not been
        imported yet, so no bindings exist for them."""

        with self._lock:
            return tuple(self._pending)

    def report(self) -> str:
        """A human-readable listing of what is installed: the sink,
        every binding applied so far, and the targets still waiting
        for their module.

        Zero-code injection means nothing in the source says the
        patching is there; this is the way to ask.
        """

        with self._lock:
            bindings = list(self._bindings)
            pending = list(self._pending)

        lines = [f"sink: {self._sink!r}" if self._sink is not None else "sink: none"]

        lines.append("applied:" if bindings else "applied: none")
        for bound in bindings:
            lines.append(f"  {bound.path}")

        if pending:
            lines.append("pending:")
            for entry in pending:
                lines.append(f"  {entry.target}")

        return "\n".join(lines)

    def suspend(self) -> None:
        """Suspend every binding this config applied: the wrappers
        stay installed, but operations pass straight through, counted
        on each binding's `suspended_calls`.

        The runtime off switch for an injected process. The state
        covers the whole config, not a snapshot: a pending entry that
        fires while suspended applies its bindings suspended too.
        """

        with self._lock:
            self._suspended = True
            bindings = list(self._bindings)

        for bound in bindings:
            bound.suspend()

    def resume(self) -> None:
        """Resume every binding this config applied, undoing
        suspend()."""

        with self._lock:
            self._suspended = False
            bindings = list(self._bindings)

        for bound in bindings:
            bound.resume()

    def revert(self) -> None:
        """Remove everything this record installed: the bindings, most
        recent first, and the sink registration.

        A post-import hook cannot be unregistered from wrapt, so a
        hook that has not fired yet is neutralised instead: when its
        module is eventually imported, it sees the record is reverted
        and applies nothing. Setup callbacks are outside this:
        whatever a callback did is its own to undo. Idempotent.
        """

        with self._lock:
            if self._reverted:
                return

            self._reverted = True
            bindings = list(self._bindings)
            self._bindings.clear()
            self._pending.clear()

        for bound in reversed(bindings):
            bound.remove()

        if self._sink is not None:
            remove_sink(self._sink)

    def _adopt(self, entry: ObserveEntry) -> None:
        with self._lock:
            self._pending.append(entry)

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"<AppliedConfig: {len(self._bindings)} bindings,"
                f" {len(self._pending)} pending, sink {self._sink!r}>"
            )


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
        inherit: bool = True,
    ) -> None:
        """Validate and hold a tracing setup, applying nothing yet.

        `observe` is the ObserveEntry rules to bind. `sink` is the
        process sink events flow to. `setup` is the SetupEntry
        callbacks to register. `capture` overrides the capture level
        on every binding the config creates, in the forms binding()
        accepts. `sample` keeps only that fraction of call trees, by
        wrapping the sink in Sample at apply time, so it requires a
        sink. `inherit=False` strips wrapture's autowrapt trigger
        from the environment after a successful apply, so Python
        processes this one launches by exec or spawn start untraced;
        the default leaves the environment alone, since launched
        workers are usually the application itself.
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

        if sink is not None:
            _reject_tapes(sink)

        if not isinstance(inherit, bool):
            raise ConfigError(f"inherit must be true or false, got {inherit!r}")

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
        self._inherit = inherit

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

    @property
    def inherit(self) -> bool:
        """Whether launched Python processes inherit the autowrapt
        trigger; False strips it after a successful apply."""

        return self._inherit

    def apply(self) -> AppliedConfig:
        """Install everything this config describes, returning the
        live record of what was installed.

        The sink is registered as a process sink first, wrapped in
        Sample when sampling is configured. Observe entries defer: a
        post-import hook is registered per target module, which fires
        immediately when the module is already imported and otherwise
        when the application imports it, so applying never imports a
        target itself, and validation that needs the module (a name
        that must exist, a match with nothing to select) happens when
        the hook fires. Setup callbacks are registered the same way.
        A target whose module is never imported never binds, which
        the returned record's `pending` shows and a warning at
        interpreter shutdown reports.

        If anything fails during apply itself, whatever had been
        installed is removed again and hooks not yet fired are
        neutralised before the error propagates, so a failed apply
        leaves nothing behind.
        """

        installed: Sink | None = None
        if self._sink is not None:
            registered = self._sink
            if self._sample is not None:
                registered = Sample(self._sample, registered)
            installed = add_sink(registered)

        record = AppliedConfig(installed)

        # While the applying flag is set, a hook firing synchronously
        # (its module already imported) propagates errors out of this
        # call; once apply returns, hooks fire inside application
        # imports, where failures warn instead, since observation must
        # never take the application down.

        record._applying = True
        try:
            for entry in self._observe:
                record._adopt(entry)
                _register_observe(entry, self._capture, record)

            for setup_entry in self._setup:
                _register_setup(setup_entry, record)
        except BaseException:
            record.revert()
            raise
        finally:
            record._applying = False

        if not self._inherit:
            _strip_bootstrap_trigger()

        _watch(record)
        return record


def _reject_tapes(sink: Sink) -> None:
    # A Tape retains every event, so a config sink, which lives for
    # the life of the process, must never be one, however deeply a
    # composition buries it. Unbounded retention has to be a
    # deliberate code-level choice, never a config file's.

    if isinstance(sink, Tape):
        raise ConfigError(
            "a Tape retains every event and cannot be a config sink;"
            " use a streaming or counting sink, and keep tapes for"
            " code that bounds their lifetime"
        )

    if isinstance(sink, Fanout):
        for inner in sink._sinks:
            _reject_tapes(inner)

    if isinstance(sink, (Filter, Depth, Sample)):
        _reject_tapes(sink._sink)


def _strip_bootstrap_trigger() -> None:
    # inherit = false: take only wrapture's own name out of the
    # autowrapt trigger list, so exec and spawn children start
    # untraced while other tools' bootstraps are untouched. Forked
    # children inherit the parent's patches through memory regardless;
    # this only governs fresh interpreters.

    value = os.environ.get("AUTOWRAPT_BOOTSTRAP")
    if value is None:
        return

    names = [name for name in value.split(",") if name and name != "wrapture"]

    if names:
        os.environ["AUTOWRAPT_BOOTSTRAP"] = ",".join(names)
    else:
        del os.environ["AUTOWRAPT_BOOTSTRAP"]


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
        members = _select_members(container, entry.match, entry.exclude)
        if not members:
            warnings.warn(
                f"{where}: match {list(entry.match)!r} selected no"
                f" members; nothing was bound for this entry",
                ConfigWarning,
                stacklevel=3,
            )

    # A redact list turns into a capture policy over the entry's
    # capture level: named parameters become the marker, everything
    # else captures at the level the config asked for.

    effective: CapturePolicy | str | None = capture
    if entry.redact:
        base = capture if capture is not None else REFERENCE
        effective = _redact(*entry.redact, level=base)

    prefix = f"{path}." if path else ""
    return [
        binding(module_name, prefix + member, capture=effective) for member in members
    ]


def _fire_observe(
    record: AppliedConfig,
    entry: ObserveEntry,
    capture: CapturePolicy | str | None,
) -> None:
    # The deferred half of an observe entry: resolve members and apply
    # bindings now that the target module exists. Runs inside the
    # import that brought the module in, so a validation failure
    # surfaces from that import statement; the entry's own partial
    # work is unwound first so the failure leaves nothing behind.

    with record._lock:
        if record._reverted:
            return
        suspended = record._suspended

    applied: list[Binding] = []
    try:
        for bound in _bindings_for(entry, capture):
            bound.apply(suspended=suspended)
            applied.append(bound)
    except Exception as exc:
        for bound in reversed(applied):
            bound.remove()

        # During apply the failure is the caller's to hear; fired
        # from an application import later, observation failing must
        # not fail the import, so the entry is dropped with a loud
        # warning instead.

        if record._applying:
            raise

        with record._lock:
            if entry in record._pending:
                record._pending.remove(entry)

        warnings.warn(
            f"observe target {entry.target!r} failed to bind when its"
            f" module was imported: {exc}. The import continues, with"
            f" nothing bound for this entry.",
            ConfigWarning,
            stacklevel=2,
        )
        return

    # Adopt the bindings unless a concurrent revert won the race, in
    # which case they come straight down again; a suspend that landed
    # mid-flight is reconciled the same way.

    with record._lock:
        reverted = record._reverted
        wanted_suspended = record._suspended
        if not reverted:
            record._bindings.extend(applied)
            if entry in record._pending:
                record._pending.remove(entry)

    if reverted:
        for bound in reversed(applied):
            bound.remove()
    elif wanted_suspended != suspended:
        for bound in applied:
            if wanted_suspended:
                bound.suspend()
            else:
                bound.resume()


def _register_observe(
    entry: ObserveEntry,
    capture: CapturePolicy | str | None,
    record: AppliedConfig,
) -> None:
    # One hook per entry on the target's module half. wrapt fires it
    # immediately for a module already imported, which is what makes
    # deferral invisible to code that applies a config late.

    module_name = entry.target.partition(":")[0]

    def hook(module: Any) -> None:
        _fire_observe(record, entry, capture)

    wrapt.register_post_import_hook(hook, module_name)


# Records with entries still waiting are watched so a target whose
# module never gets imported is reported at interpreter shutdown
# rather than silently never binding.

_watched: list[AppliedConfig] = []
_watch_lock = threading.Lock()
_report_registered = False


def _watch(record: AppliedConfig) -> None:
    global _report_registered

    if not record.pending:
        return

    with _watch_lock:
        _watched.append(record)

        if not _report_registered:
            _report_registered = True
            atexit.register(_report_never_fired)


def _report_never_fired() -> None:
    with _watch_lock:
        records = list(_watched)

    targets = sorted({entry.target for record in records for entry in record.pending})

    if targets:
        warnings.warn(
            f"observe targets never bound: {', '.join(targets)}; their"
            f" modules were never imported over the life of the process"
            f" (a misspelled target, or a code path never reached)",
            ConfigWarning,
            stacklevel=2,
        )


def _register_setup_hook(
    trigger: str,
    resolve: Callable[[], Any],
    describe: str,
    options: Mapping[str, Any],
    record: AppliedConfig,
) -> None:
    # The shared trampoline behind both setup forms. Resolution of the
    # handler is deferred to the moment the hook fires: by then the
    # trigger module is mid-import anyway, so importing operator code
    # cannot defeat patch-before-import ordering. wrapt fires the hook
    # immediately when the module is already imported, so an entry
    # never silently waits forever on a module that is already there.

    def trampoline(module: Any) -> None:
        try:
            handler = resolve()

            if not callable(handler):
                raise ConfigError(
                    f"{describe} resolved to {handler!r}, which is not callable"
                )

            handler(module, **options)
        except Exception as exc:
            # Same posture as observe entries: loud during apply,
            # warn-and-continue from inside an application import.

            if record._applying:
                raise

            warnings.warn(
                f"{describe} raised: {exc!r}. The import continues;"
                f" whatever the handler did before raising stands.",
                ConfigWarning,
                stacklevel=2,
            )

    wrapt.register_post_import_hook(trampoline, trigger)


def _register_setup(entry: SetupEntry, record: AppliedConfig) -> None:
    if entry.group:
        # The group form: an installed package declares its handler
        # family as entry points, entry name the trigger module and
        # target the handler, the same shape wrapt's own hook
        # discovery reads. Discovery happens now, from metadata alone,
        # importing nothing; each handler still resolves only when
        # its module arrives, and every handler in the family gets
        # the entry's options.

        points = list(metadata.entry_points(group=entry.group))

        if not points:
            raise ConfigError(
                f"setup group {entry.group!r} has no entry points: a"
                f" misspelled group, or the package declaring it is not"
                f" installed"
            )

        for point in points:
            _register_setup_hook(
                trigger=point.name,
                resolve=point.load,
                describe=(
                    f"setup group {entry.group!r} handler"
                    f" {point.value!r} for {point.name!r}"
                ),
                options=entry.options,
                record=record,
            )
        return

    _register_setup_hook(
        trigger=entry.module,
        resolve=lambda: _resolve_reference(
            entry.call, key="call", where=f"setup for {entry.module!r}"
        ),
        describe=f"setup callback {entry.call!r} for {entry.module!r}",
        options=entry.options,
        record=record,
    )


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
        set(document)
        - {"pythonpath", "capture", "sample", "observe", "sink", "setup", "inherit"}
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
            optional=("name", "match", "exclude", "redact"),
        )
        observe.append(ObserveEntry(**table))

    # A setup table works like the sink table: module, call and group
    # are the reserved keys, and everything else rides through to the
    # handler as options.

    setup: list[SetupEntry] = []
    for raw in document.get("setup", ()):
        if not isinstance(raw, dict):
            raise ConfigError(f"[[setup]] entries must be tables, got {raw!r}")

        table = dict(raw)
        setup.append(
            SetupEntry(
                module=table.pop("module", ""),
                call=table.pop("call", ""),
                group=table.pop("group", ""),
                options=table,
            )
        )

    sink = _build_sink(document["sink"]) if "sink" in document else None

    return Config(
        observe=observe,
        sink=sink,
        setup=setup,
        capture=document.get("capture"),
        sample=document.get("sample"),
        inherit=document.get("inherit", True),
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
