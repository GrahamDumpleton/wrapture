"""A whole tracing setup as one value: the config primitive and the
TOML file loader.

The programmatic form is the primitive: a Config holds observe entries
saying which members of which targets to bind, the sink events flow to,
instrumentation to apply as its trigger modules are imported, and the
capture and sampling settings, and apply() installs the lot. The TOML
file is a thin loader over it; everything the file can say is
expressible in code, and the programmatic path can additionally pass
live objects where the file is limited to what TOML can spell.

Failures are loud. A file that says something the schema does not
allow, a reference that does not resolve, a named member that does not
exist: each raises ConfigError rather than being skipped. The one
softer case is a match pattern that selects nothing, which warns with
ConfigWarning, because an empty selection may simply mean the code
being observed has moved on.

A config file can name arbitrary code to run, through sink factories
and instrumentation, so loading one is equivalent to executing code:
the trust boundary is write access to the file, exactly as it is for
any other file the process imports.
"""

from __future__ import annotations

import atexit
import fnmatch
import importlib
import os
import sys
import threading
import tomllib
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import wrapt

from . import trace as _trace
from .bindings import Binding, _select_members, binding
from .capture import REFERENCE, CapturePolicy, _resolve_policy
from .capture import redact as _redact
from .collectors import Aggregate, Counter
from .events import CATEGORIES, Event, _check_category
from .exceptions import ConfigError, ConfigWarning
from .filters import RequestFilter, filter_requests
from .instrumentations import (
    Instrumentation,
    InstrumentEntry,
    _Applied,
    _apply_planned,
    _plan_entries,
    _Planned,
    _revert_applied,
)
from .logs import LogCapture, capture_logs
from .outputs import OutputPath
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
from .timeline import Tape, seed_data
from .windows import Window


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


def _request_filter(table: Any, where: str) -> RequestFilter:
    # An observe entry's requests table is filter_requests() spelt as
    # TOML: accept and ignore sub-tables of request field to one glob
    # or a list of globs. The helper checks the fields and shapes; its
    # errors come back as config errors naming the entry.

    if not isinstance(table, Mapping):
        raise ConfigError(
            f"{where}: requests must be a table of accept and ignore tables,"
            f" got {table!r}"
        )

    unknown = sorted(set(table) - {"accept", "ignore"})
    if unknown:
        raise ConfigError(
            f"{where}: requests: unknown keys {unknown}; the keys are accept and ignore"
        )

    try:
        return filter_requests(accept=table.get("accept"), ignore=table.get("ignore"))
    except (TypeError, ValueError) as exc:
        detail = str(exc).removeprefix("filter_requests(): ")
        raise ConfigError(f"{where}: requests: {detail}") from None


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

    `mode` is normally empty, leaving each binding to detect its own.
    The accepted values are "wsgi" and "asgi", which wrap the named
    members as applications of that protocol in the recording
    middleware; each requires `name`, since a pattern must never
    bulk-install middleware. For a wsgi or asgi entry, `redact` names
    query string parameters.

    `trace = true` makes this entry's bindings mint a trace identity
    at roots even when the `[trace]` table disables the mechanism
    process-wide: the case-by-case re-enable. Only operations mint,
    so the mark lands on the entry's call and request bindings, and
    an entry that binds nothing but attributes is rejected.

    `data` is a table of static tags every event from this entry's
    bindings starts with, string keys to scalars or flat lists of
    scalars, the shape `binding(data=)` takes; it is the only way to
    annotate events from code the config observes but does not own.

    `requests` is filter_requests() spelt as TOML, for a wsgi or asgi
    entry only: a table with `accept` and/or `ignore` sub-tables, each
    mapping a request field (method, path, scheme, protocol, remote)
    to one glob or a list of globs. It becomes the entry's `when=`
    with `tree=True`, so a declined request records nothing beneath
    it either.

    `leaf = true` makes the entry's bindings leaves, recording their
    own events and silencing the spans beneath them, and `category`
    names what kind of operation they are ("external", "database",
    "datastore", "messaging", "task", "server", "consumer" or
    "template"), the `binding(leaf=, category=)` options as TOML.
    """

    target: str
    name: str | Sequence[str] = ()
    match: str | Sequence[str] = ()
    exclude: str | Sequence[str] = ()
    redact: str | Sequence[str] = ()
    mode: str = ""
    trace: bool = False
    data: Mapping[str, Any] = field(default_factory=dict)
    requests: Mapping[str, Any] = field(default_factory=dict)
    leaf: bool = False
    category: str = ""

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

        if self.mode and self.mode not in ("wsgi", "asgi"):
            raise ConfigError(
                f"{where}: mode must be omitted, 'wsgi' or 'asgi', got {self.mode!r}"
            )

        if self.mode and not self.name:
            raise ConfigError(
                f"{where}: mode requires name; a pattern must never"
                f" bulk-install middleware"
            )

        # A requests table names request fields, which only a request
        # binding has; its content is checked now, by building the
        # filter it stands for, so a bad table fails at load.

        if self.requests:
            if not self.mode:
                raise ConfigError(
                    f"{where}: requests applies to a wsgi or asgi entry; add"
                    f" mode = 'wsgi' or 'asgi'"
                )

            _request_filter(self.requests, where)

        if not isinstance(self.trace, bool):
            raise ConfigError(
                f"{where}: trace must be true or false, got {self.trace!r}"
            )

        if not isinstance(self.leaf, bool):
            raise ConfigError(f"{where}: leaf must be true or false, got {self.leaf!r}")

        if not isinstance(self.category, str):
            raise ConfigError(
                f"{where}: category must be a string, got {self.category!r}"
            )

        if self.category:
            try:
                _check_category(self.category)
            except ValueError:
                raise ConfigError(
                    f"{where}: category must be one of {', '.join(CATEGORIES)},"
                    f" got {self.category!r}"
                ) from None

        # Seed data takes the one shape every declaration point takes;
        # the shared check speaks TypeError, the config speaks
        # ConfigError.

        try:
            object.__setattr__(self, "data", seed_data(self.data))
        except TypeError as exc:
            raise ConfigError(f"{where}: {exc}") from None


class AppliedConfig:
    """The live record of what one Config.apply() installed.

    Observe entries and instrumentation defer: apply() registers a
    post-import hook per target or trigger module, which fires
    immediately for a module already imported and otherwise when the
    application imports it. The bindings recorded here therefore grow
    over the life of the process; `pending` names the observe entries
    still waiting for their module, `instrumentations` is the live
    record of each applied instrumentation, `report()` renders the
    whole picture, and `revert()` takes everything down again.
    """

    def __init__(self, sink: Sink | None, windows: Sequence[Window] = ()) -> None:
        self._lock = threading.Lock()
        self._bindings: list[Binding] = []
        self._pending: list[ObserveEntry] = []
        self._captures: list[LogCapture] = []
        self._instrumentations: list[_Applied] = []
        self._disabled: tuple[InstrumentEntry, ...] = ()
        self._trace_previous: bool | None = None
        self._sink = sink
        self._windows = tuple(windows)
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
        """The process sink the config registered."""

        return self._sink

    @property
    def windows(self) -> tuple[Window, ...]:
        """The windows the config started."""

        return self._windows

    @property
    def pending(self) -> tuple[ObserveEntry, ...]:
        """The observe entries whose target module has not been
        imported yet, so no bindings exist for them."""

        with self._lock:
            return tuple(self._pending)

    @property
    def captures(self) -> tuple[LogCapture, ...]:
        """The log captures the config applied."""

        with self._lock:
            return tuple(self._captures)

    @property
    def instrumentations(self) -> tuple[Instrumentation, ...]:
        """The instrumentations this config applied, in application
        order: each instance carries its settings in effect, the target
        version found, the triggers applied and pending, and its
        removability claim."""

        with self._lock:
            return tuple(holder.instance for holder in self._instrumentations)

    def report(self) -> str:
        """A human-readable listing of what is installed: the sink,
        every binding applied so far, the instrumentations with their
        triggers fired and pending, and the targets still waiting for
        their module.

        Zero-code injection means nothing in the source says the
        patching is there; this is the way to ask.
        """

        with self._lock:
            bindings = list(self._bindings)
            pending = list(self._pending)
            captures = list(self._captures)
            instances = [holder.instance for holder in self._instrumentations]
            disabled = list(self._disabled)

        lines = [f"sink: {self._sink!r}" if self._sink is not None else "sink: none"]

        if self._windows:
            lines.append("windows:")
            for window in self._windows:
                lines.append(f"  {window!r}: {window.describe()}")

        lines.append("applied:" if bindings else "applied: none")
        for bound in bindings:
            lines.append(f"  {bound.path}")

        if instances or disabled:
            lines.append("instrumentation:")
            for instance in instances:
                lines.append(f"  {instance._describe()}")
            for off in disabled:
                lines.append(f"  {off.label}: disabled")

        if captures:
            lines.append("log:")
            for capture in captures:
                lines.append(f"  {','.join(capture.names)} >={capture.level}")

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
            captures = list(self._captures)

        for bound in bindings:
            bound.suspend()

        for capture in captures:
            capture.suspend()

    def resume(self) -> None:
        """Resume every binding this config applied, undoing
        suspend()."""

        with self._lock:
            self._suspended = False
            bindings = list(self._bindings)
            captures = list(self._captures)

        for bound in bindings:
            bound.resume()

        for capture in captures:
            capture.resume()

    def revert(self) -> None:
        """Remove everything this record installed: the bindings, most
        recent first, then the instrumentations in reverse application
        order, the log captures, the windows, and the sink
        registration.

        A post-import hook cannot be unregistered from wrapt, so a
        hook that has not fired yet is neutralised instead: when its
        module is eventually imported, it sees the record is reverted
        and applies nothing. An instrumentation is removed through its
        own remove(), trigger by trigger; one that declared itself not
        removable is warned about, since whatever it patched beyond
        its undo callbacks may remain. Idempotent.
        """

        with self._lock:
            if self._reverted:
                return

            self._reverted = True
            bindings = list(self._bindings)
            captures = list(self._captures)
            applied = list(self._instrumentations)
            self._bindings.clear()
            self._pending.clear()
            self._captures.clear()
            self._instrumentations.clear()

        for bound in reversed(bindings):
            bound.remove()

        _revert_applied(applied)

        for capture in reversed(captures):
            capture.remove()

        if self._trace_previous is not None:
            _trace._restore(self._trace_previous)
            self._trace_previous = None

        for window in self._windows:
            window.stop()

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
    instrumentation to apply, and how much to capture and keep.

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
        windows: Sequence[Window] = (),
        instrument: Sequence[InstrumentEntry] = (),
        log: Sequence[LogCapture] = (),
        trace: Mapping[str, Any] | None = None,
        capture: CapturePolicy | str | None = None,
        inherit: bool = True,
    ) -> None:
        """Validate and hold a tracing setup, applying nothing yet.

        `observe` is the ObserveEntry rules to bind. `sink` is the
        process sink events flow to; several destinations are one
        Fanout, and sampling, depth limiting and filtering are the
        combinators wrapped around the sink they gate. `windows` are
        the Window objects to start, whose contents listen or collect
        only while a run is open. `instrument` is the InstrumentEntry
        list naming the instrumentations to apply; each is resolved
        and its settings validated now, and the enabled entries are
        checked against each other for conflicts and requirements,
        so a bad entry fails the build rather than the apply. `log`
        is the LogCapture selections to apply, as capture_logs()
        returns them. `trace` is the trace identity settings, a
        mapping with an `enabled` (default True) key, applied
        process-wide and restored on revert; None leaves the
        process-wide setting alone. `capture` overrides the capture
        level on every binding the config creates, in the forms
        binding() accepts. `inherit=False` strips wrapture's autowrapt
        trigger from the environment after a successful apply, so
        Python processes this one launches by exec or spawn start
        untraced; the default leaves the environment alone, since
        launched workers are usually the application itself.
        """

        for entry in observe:
            if not isinstance(entry, ObserveEntry):
                raise ConfigError(
                    f"observe entries must be ObserveEntry instances, got {entry!r}"
                )

        for capture_entry in log:
            if not isinstance(capture_entry, LogCapture):
                raise ConfigError(
                    f"log entries must be LogCapture instances, as"
                    f" capture_logs() returns them, got {capture_entry!r}"
                )

        # Instrumentation resolves now: the class loads (importing only
        # wrapture, by contract), the settings are checked against its
        # declaration, and the enabled entries are checked against each
        # other.

        self._instrument = tuple(instrument)
        self._instrument_planned: tuple[_Planned, ...] = _plan_entries(instrument)

        if sink is not None and not isinstance(sink, Sink):
            raise ConfigError(f"sink must be a Sink, got {sink!r}")

        if sink is not None:
            _reject_tapes(sink)

        for window in windows:
            if not isinstance(window, Window):
                raise ConfigError(f"windows must be Window instances, got {window!r}")
            for inner in window.sinks:
                _reject_tapes(inner)

        if not isinstance(inherit, bool):
            raise ConfigError(f"inherit must be true or false, got {inherit!r}")

        if capture is not None:
            try:
                _resolve_policy(capture)
            except ValueError as exc:
                raise ConfigError(f"capture: {exc}") from None

        # The trace table normalises to its one setting now, so a
        # bad table fails the load rather than the apply.

        self._trace: bool | None = None
        if trace is not None:
            if not isinstance(trace, Mapping):
                raise ConfigError(f"trace must be a table, got {trace!r}")

            unknown = sorted(set(trace) - {"enabled"})
            if unknown:
                raise ConfigError(f"trace: unknown keys {unknown}")

            enabled = trace.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ConfigError(
                    f"trace: enabled must be true or false, got {enabled!r}"
                )

            self._trace = enabled

        self._observe = tuple(observe)
        self._sink = sink
        self._windows = tuple(windows)
        self._log = tuple(log)
        self._capture = capture
        self._inherit = inherit

    @property
    def observe(self) -> tuple[ObserveEntry, ...]:
        """The observation rules this config holds."""

        return self._observe

    @property
    def sink(self) -> Sink | None:
        """The sink this config registers."""

        return self._sink

    @property
    def windows(self) -> tuple[Window, ...]:
        """The windows this config starts."""

        return self._windows

    @property
    def instrument(self) -> tuple[InstrumentEntry, ...]:
        """The instrumentation entries this config holds, enabled and
        disabled alike."""

        return self._instrument

    @property
    def capture(self) -> CapturePolicy | str | None:
        """The capture override applied to every binding, or None."""

        return self._capture

    @property
    def inherit(self) -> bool:
        """Whether launched Python processes inherit the autowrapt
        trigger; False strips it after a successful apply."""

        return self._inherit

    def apply(self) -> AppliedConfig:
        """Install everything this config describes, returning the
        live record of what was installed.

        The sink is registered as a process sink first and the windows
        are started. Observe entries defer: a
        post-import hook is registered per target module, which fires
        immediately when the module is already imported and otherwise
        when the application imports it, so applying never imports a
        target itself, and validation that needs the module (a name
        that must exist, a match with nothing to select) happens when
        the hook fires. Instrumentation is registered the same way,
        one hook per trigger module, after its conflicts with any
        other applied config are checked and its configure() has run.
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
            installed = add_sink(self._sink)

        record = AppliedConfig(installed, self._windows)
        record._disabled = tuple(
            entry for entry in self._instrument if not entry.enabled
        )

        # While the applying flag is set, a hook firing synchronously
        # (its module already imported) propagates errors out of this
        # call; once apply returns, hooks fire inside application
        # imports, where failures warn instead, since observation must
        # never take the application down.

        record._applying = True
        try:
            for window in self._windows:
                window.start()

            for entry in self._observe:
                record._adopt(entry)
                _register_observe(entry, self._capture, record)

            _apply_planned(self._instrument_planned, record)

            # Log captures apply immediately: the logging module is
            # always importable, so there is nothing to defer on.

            for capture_entry in self._log:
                capture_entry.apply()
                with record._lock:
                    record._captures.append(capture_entry)

            # Trace settings apply process-wide, remembering what they
            # replaced so revert can restore it.

            if self._trace is not None:
                record._trace_previous = _trace._configure(self._trace)
        except BaseException:
            record.revert()
            raise
        finally:
            record._applying = False

        if not self._inherit:
            _strip_bootstrap_trigger()

        _watch(record)
        return record


def _innermost(sink: Sink) -> Sink:
    # The sink beneath any gating combinators the file wrapped on.

    while isinstance(sink, (Filter, Depth, Sample)):
        sink = sink._sink
    return sink


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

    # A requests table is the entry's when=, reaching the whole tree:
    # an ignored request drops everything beneath it along with itself.

    request_filter = _request_filter(entry.requests, where) if entry.requests else None

    prefix = f"{path}." if path else ""
    bound = [
        binding(
            module_name,
            prefix + member,
            capture=effective,
            mode=entry.mode or None,
            data=entry.data or None,
            when=request_filter,
            tree=request_filter is not None,
            leaf=entry.leaf,
            category=entry.category or None,
        )
        for member in members
    ]

    # trace = true marks each binding as a trace root, consulted when
    # a root event decides whether to mint an identity, so the entry
    # re-enables tracing under a process-wide disable. Minting is
    # gated by kind: traces start at declared operation boundaries
    # (calls and requests), never at attribute accesses, so the mark
    # goes only on bindings that can produce those events, and an
    # entry whose bindings are all attribute accesses carries a flag
    # that can never act, which is rejected rather than ignored.

    if entry.trace:
        minting = [each for each in bound if each.mode in ("callable", "wsgi", "asgi")]

        if bound and not minting:
            raise ConfigError(
                f"{where}: trace = true can never act here; the entry"
                f" binds only attribute accesses, and traces start at"
                f" declared operation boundaries, not attribute accesses"
            )

        for each in minting:
            each._trace_root = True

    return bound


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


# The sinks a config file can name by short name. Deliberately only the
# event streams: the combinators are reached through the [[sink]] list
# (several entries fan out) and the gating keys on each entry (sample,
# depth, filter), and anything else through a module:attr factory.

_BUILTIN_SINKS: dict[str, Callable[..., Sink]] = {
    "printer": Printer,
    "jsonlines": JSONLines,
}

# The collectors a [[window.collect]] entry can name by short name.
# Naming one in [[sink]] is an error pointing at [[window]]: a
# collector needs a run to close before it has anything to report.

_BUILTIN_COLLECTORS: dict[str, Callable[..., Sink]] = {
    "aggregate": Aggregate,
    "counter": Counter,
}

_FILTER_FIELDS = ("kind", "path", "label", "category")


def _filter_predicate(spec: Any, *, where: str) -> Callable[[Event], bool]:
    # A sink's filter key: either a module:attr reference to a
    # predicate, or a table of event field to pattern, matching an
    # event when every field matches (fnmatchcase on the string form,
    # a list meaning any of them).

    if isinstance(spec, str):
        predicate = _resolve_reference(spec, key="filter", where=where)
        if not callable(predicate):
            raise ConfigError(
                f"{where}: filter = {spec!r} resolved to {predicate!r},"
                f" which is not callable"
            )
        return cast(Callable[[Event], bool], predicate)

    if not isinstance(spec, dict) or not spec:
        raise ConfigError(
            f"{where}: filter must be a table of event fields to match, such"
            f' as {{ kind = "request" }}, or a module:attr predicate, got'
            f" {spec!r}"
        )

    matchers: list[tuple[str, tuple[str, ...]]] = []
    for name, value in spec.items():
        if name not in _FILTER_FIELDS:
            raise ConfigError(
                f"{where}: filter field {name!r} is not one of {_FILTER_FIELDS}"
            )
        matchers.append((name, _strings(value, key=f"filter.{name}", where=where)))

    def matches(event: Event) -> bool:
        for name, patterns in matchers:
            actual = getattr(event, name, None)
            text = "" if actual is None else str(actual)
            if not any(fnmatch.fnmatchcase(text, pattern) for pattern in patterns):
                return False
        return True

    return matches


def _build_sink(
    table: Any,
    *,
    anchor: str | None = None,
    where: str = "[[sink]]",
    collectors: bool = False,
) -> Sink:
    # Construct the sink one [[sink]] table describes: a builtin short
    # name or a module:attr factory, called with the remaining keys as
    # keyword arguments, resolved and validated now because a sink
    # must exist before events can flow. The gating keys (sample,
    # depth, filter) wrap the built sink in a fixed order, sample
    # outermost then depth then filter, whatever their order in the
    # file; a `to` list builds inner sinks for a factory that routes
    # to others.

    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table, got {table!r}")

    spec = dict(table)
    reference = spec.pop("type", None)
    sample = spec.pop("sample", None)
    depth = spec.pop("depth", None)
    filter_spec = spec.pop("filter", None)
    inner = spec.pop("to", None)

    if not isinstance(reference, str) or not reference:
        raise ConfigError(
            f"{where} requires a type key naming a builtin sink or a module:attr"
            f" factory"
        )

    if sample is not None:
        if (
            not isinstance(sample, (int, float))
            or isinstance(sample, bool)
            or not 0.0 <= sample <= 1.0
        ):
            raise ConfigError(
                f"{where}: sample must be a number between 0.0 and 1.0, got {sample!r}"
            )

    if depth is not None:
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
            raise ConfigError(
                f"{where}: depth must be a positive integer, got {depth!r}"
            )

    predicate = (
        _filter_predicate(filter_spec, where=where) if filter_spec is not None else None
    )

    # A builtin sink's relative output path anchors to the config
    # file's directory, as pythonpath entries do, so the file says
    # where its output goes independently of the process's working
    # directory. Only the directory part is anchored: the template
    # variables in the file name expand when the file is opened.

    if anchor is not None and reference in _BUILTIN_SINKS:
        path = spec.get("path")
        if isinstance(path, str) and path and not os.path.isabs(path):
            spec["path"] = os.path.normpath(os.path.join(anchor, path))

    if ":" in reference:
        factory = _resolve_reference(reference, key="type", where=where)
        if not callable(factory):
            raise ConfigError(
                f"{where}: type = {reference!r} resolved to {factory!r},"
                f" which is not callable"
            )
    else:
        if inner is not None:
            raise ConfigError(
                f"{where}: to only applies to a module:attr factory that"
                f" routes to other sinks; {reference!r} is a builtin sink"
            )

        builtins = dict(_BUILTIN_SINKS)
        if collectors:
            builtins.update(_BUILTIN_COLLECTORS)
        elif reference in _BUILTIN_COLLECTORS:
            raise ConfigError(
                f"{where}: {reference!r} is a collector, which reports at the"
                f" close of a window's run; put it under [[window.collect]]"
                f" rather than [[sink]]"
            )

        try:
            factory = builtins[reference]
        except KeyError:
            kinds = "builtin sink or collector" if collectors else "builtin sink"
            raise ConfigError(
                f"{where}: type {reference!r} is not a {kinds}"
                f" (one of {sorted(builtins)}) or a module:attr factory"
            ) from None

    if inner is not None:
        if not isinstance(inner, list) or not inner:
            raise ConfigError(
                f"{where}: to must be a list of sink tables, written as"
                f" [[{where.strip('[]')}.to]] entries"
            )
        spec["to"] = [
            _build_sink(item, anchor=anchor, where=f"{where} to entry {index}")
            for index, item in enumerate(inner, 1)
        ]

    try:
        built = factory(**spec)
    except Exception as exc:
        raise ConfigError(
            f"{where}: constructing type {reference!r} failed: {exc}"
        ) from exc

    if not isinstance(built, Sink):
        raise ConfigError(
            f"{where}: type {reference!r} returned {type(built).__name__!r}, not a Sink"
        )

    sink: Sink = built

    if predicate is not None:
        sink = Filter(predicate, sink)
    if depth is not None:
        sink = Depth(depth, sink)
    if sample is not None:
        sink = Sample(sample, sink)

    return sink


_WINDOW_KEYS = {
    "name",
    "after",
    "for",
    "every",
    "times",
    "align",
    "at",
    "jitter",
    "on_signal",
    "on_file",
    "report",
    "collect",
}


def _build_window(table: Any, *, index: int, anchor: str | None) -> Window:
    # One [[window]] table: the trigger keys, a report template, and
    # a collect list in the sink grammar. The Window constructor does
    # the semantic validation; this maps TOML shapes and errors.

    where = f"[[window]] entry {index}"

    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table, got {table!r}")

    unknown = sorted(set(table) - _WINDOW_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown keys {unknown}")

    name = table.get("name", f"window{index}")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{where}: name must be a non-empty string")
    where = f"[[window]] {name!r}"

    contents = table.get("collect", [])
    if not isinstance(contents, list):
        raise ConfigError(
            f"{where}: collect must be a list of tables, written as"
            f" [[window.collect]] entries"
        )

    collect = [
        _build_sink(
            item,
            anchor=anchor,
            where=f"{where} collect entry {position}",
            collectors=True,
        )
        for position, item in enumerate(contents, 1)
    ]

    report = table.get("report")
    if report is not None:
        if not isinstance(report, str) or not report:
            raise ConfigError(f"{where}: report must be a path template string")
        if anchor is not None and not os.path.isabs(report):
            report = os.path.normpath(os.path.join(anchor, report))

    align = table.get("align", False)
    if not isinstance(align, bool):
        raise ConfigError(f"{where}: align must be true or false")

    on_file = table.get("on_file")
    if on_file is not None:
        if not isinstance(on_file, str) or not on_file:
            raise ConfigError(f"{where}: on_file must be a path string")
        if anchor is not None and not os.path.isabs(on_file):
            on_file = os.path.normpath(os.path.join(anchor, on_file))

    try:
        return Window(
            name=name,
            after=table.get("after"),
            duration=table.get("for"),
            every=table.get("every"),
            times=table.get("times"),
            align=align,
            at=table.get("at"),
            jitter=table.get("jitter"),
            on_signal=table.get("on_signal"),
            on_file=on_file,
            collect=collect,
            report=report,
        )
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _build_sinks(value: Any, *, anchor: str | None = None) -> Sink:
    # The [[sink]] list: several entries fan out implicitly, so the
    # list is the fanout and a single entry is a list of one. A [sink]
    # table is the one likely misspelling, named in the error.

    if isinstance(value, dict):
        raise ConfigError(
            "sink is a list of tables: write each destination as a [[sink]]"
            " entry, not a [sink] table"
        )

    if not isinstance(value, list) or not value:
        raise ConfigError(f"sink must be a list of [[sink]] tables, got {value!r}")

    built = [
        _build_sink(table, anchor=anchor, where=f"[[sink]] entry {index}")
        for index, table in enumerate(value, 1)
    ]

    # The window variables have nothing to name outside a window, so a
    # top-level path using them fails here rather than at first open.

    for index, sink in enumerate(built, 1):
        path = getattr(_innermost(sink), "_path", None)
        if isinstance(path, OutputPath) and path.windowed:
            raise ConfigError(
                f"[[sink]] entry {index}: path {path.template!r} uses window"
                f" variables ({{window}}, {{first}}, {{run}}), which only have"
                f" a value inside a [[window]]"
            )

    if len(built) == 1:
        return built[0]
    return Fanout(*built)


def _build_otel(table: Any) -> Sink | None:
    # The first-class [otel] table: its presence opts in to
    # OpenTelemetry export, with enabled = false accepted so a stanza
    # can be kept in the file but switched off. The nested per-signal
    # tables carry the sink factory's vocabulary unchanged; there is
    # exactly one export destination blessed with a table of its own,
    # and every other destination remains a [[sink]] entry.

    if not isinstance(table, dict):
        raise ConfigError(f"otel must be a table, got {table!r}")

    known = {
        "enabled",
        "service_name",
        "signals",
        "traces",
        "metrics",
        "logs",
        "environment",
        "exceptions",
    }
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"otel: unknown keys {unknown}")

    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"otel: enabled must be true or false, got {enabled!r}")

    if not enabled:
        return None

    # The import is deferred to here so base wrapture never touches
    # the subpackage: a missing extra fails the load, where a broken
    # sink already fails loudly, with the fix named rather than a
    # bare ModuleNotFoundError.

    try:
        from . import otel
    except ImportError as exc:
        raise ConfigError(
            f"the [otel] table needs the OpenTelemetry packages, which are"
            f" not installed; install the wrapture[otel] extra ({exc})"
        ) from exc

    spec = {key: value for key, value in table.items() if key != "enabled"}

    try:
        built = otel.sink(**spec)
    except Exception as exc:
        raise ConfigError(f"otel: building the export sink failed: {exc}") from exc

    return built


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


_KNOWN_KEYS = {
    "pythonpath",
    "capture",
    "observe",
    "sink",
    "otel",
    "window",
    "instrument",
    "log",
    "trace",
    "inherit",
}


def _apply_pythonpath(document: Mapping[str, Any], location: str) -> None:
    # Relative pythonpath entries anchor to the config file's own
    # directory, not the process working directory, so a config that
    # ships next to its operator code stays self-contained wherever
    # the process launches from. Prepended in order, ahead of
    # everything already on the path.

    if "pythonpath" not in document:
        return

    anchor = os.path.dirname(os.path.abspath(location))
    entries = _strings(document["pythonpath"], key="pythonpath", where=location)

    for directory in reversed(entries):
        if not os.path.isabs(directory):
            directory = os.path.normpath(os.path.join(anchor, directory))
        sys.path.insert(0, directory)


def _instrument_entries(document: Mapping[str, Any]) -> list[InstrumentEntry]:
    # An [[instrument]] table has one reserved key, name, plus the
    # enabled switch and the optional triggers subset; everything else
    # is a setting of the named instrumentation, validated against its
    # declaration when the Config is built.

    entries: list[InstrumentEntry] = []

    tables = document.get("instrument", ())
    if isinstance(tables, dict):
        raise ConfigError(
            "instrument is a list of tables: write each as an [[instrument]]"
            " entry, not an [instrument] table"
        )

    for raw in tables:
        if not isinstance(raw, dict):
            raise ConfigError(f"[[instrument]] entries must be tables, got {raw!r}")

        table = dict(raw)
        name = table.pop("name", None)
        if not isinstance(name, str) or not name:
            raise ConfigError(
                "[[instrument]]: the name key is required, naming a registered"
                " instrumentation (optionally as name@distribution) or a"
                " module:attr reference to an Instrumentation class"
            )

        enabled = table.pop("enabled", True)
        triggers = table.pop("triggers", ())
        entries.append(
            InstrumentEntry(name, enabled=enabled, settings=table, triggers=triggers)
        )

    return entries


def _config_from(document: Any, location: str) -> Config:
    # Build a Config from a parsed TOML document, applying the one
    # load-time side effect first: pythonpath entries go onto sys.path
    # before any reference resolves, so the references may name code
    # those directories provide.

    if not isinstance(document, dict):
        raise ConfigError(f"{location}: config must be a TOML table")

    unknown = sorted(set(document) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(f"{location}: unknown config keys {unknown}")

    _apply_pythonpath(document, location)

    observe: list[ObserveEntry] = []
    for raw in document.get("observe", ()):
        table = _entry_table(
            raw,
            section="[[observe]]",
            required=("target",),
            optional=(
                "name",
                "match",
                "exclude",
                "redact",
                "mode",
                "trace",
                "data",
                "requests",
                "leaf",
                "category",
            ),
        )
        observe.append(ObserveEntry(**table))

    instrument = _instrument_entries(document)

    # A [[log]] entry is capture_logs() spelt as TOML: the keys are
    # its arguments, all optional, and the defaults match.

    log: list[LogCapture] = []
    for raw in document.get("log", ()):
        table = _entry_table(
            raw,
            section="[[log]]",
            required=(),
            optional=("name", "level", "exclude", "exclude_message"),
        )
        try:
            log.append(capture_logs(**table))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"[[log]]: {exc}") from None

    anchor = os.path.dirname(os.path.abspath(location))

    sink = None
    if "sink" in document:
        sink = _build_sinks(document["sink"], anchor=anchor)

    # The [otel] table's export sink registers ahead of everything
    # the [[sink]] list builds, which then stacks in file order: the
    # OTel sink must hear a root event before any other sink can
    # observe its trace identity.

    if "otel" in document:
        otel_sink = _build_otel(document["otel"])
        if otel_sink is not None:
            if sink is None:
                sink = otel_sink
            elif isinstance(sink, Fanout):
                sink = Fanout(otel_sink, *sink._sinks)
            else:
                sink = Fanout(otel_sink, sink)

    windows: list[Window] = []
    if "window" in document:
        tables = document["window"]
        if isinstance(tables, dict):
            raise ConfigError(
                "window is a list of tables: write each as a [[window]] entry"
            )
        if not isinstance(tables, list):
            raise ConfigError(
                f"window must be a list of [[window]] tables, got {tables!r}"
            )
        for index, table in enumerate(tables, 1):
            windows.append(_build_window(table, index=index, anchor=anchor))

    return Config(
        observe=observe,
        sink=sink,
        windows=windows,
        instrument=instrument,
        log=log,
        trace=document.get("trace"),
        capture=document.get("capture"),
        inherit=document.get("inherit", True),
    )


def _read_document(path: str | os.PathLike[str]) -> tuple[Any, str]:
    # Parse a config file into its document and location: the whole
    # file for a wrapture config, the [tool.wrapture] table for a
    # pyproject.toml.

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

    return document, location


def load_config(path: str | os.PathLike[str]) -> Config:
    """Load a TOML config file into a Config, without applying it.

    The file is either a wrapture config file, whose whole content is
    the config, or a pyproject.toml, whose [tool.wrapture] table is.
    Loading has three immediate effects beyond parsing: pythonpath
    entries are prepended to sys.path, the [sink] table's sink is
    constructed, and each [[instrument]] entry's class is imported and
    its settings validated, all because later stages depend on them
    existing. Anything the schema does not allow raises ConfigError.
    """

    document, location = _read_document(path)
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
