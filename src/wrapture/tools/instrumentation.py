"""The instrumentation command: list the instrumentation installed in
this environment, or write [[instrument]] entries for it.

    python -m wrapture.tools instrumentation [--config PATH] [--verbose]
    python -m wrapture.tools instrumentation --toml [--enabled] [--config PATH]

Reads class data alone, applying nothing: every class registered in
the wrapture.instrumentation entry point group is loaded (which, by
contract, imports only wrapture) and described from its declaration
and its distribution's metadata. With --config, the file's own
reference-form entries are listed too, pythonpath applied, and the
entries the file selects are marked. --toml writes a template instead:
one [[instrument]] entry per installed instrumentation, disabled, with
every setting as a commented-out line at its default, so un-commenting
a line is the whole act of configuring.
"""

from __future__ import annotations

import json
import sys
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, NoReturn

from ..config import _apply_pythonpath, _instrument_entries, _read_document
from ..exceptions import ConfigError, ConfigWarning
from ..instrumentations import (
    Instrumentation,
    InstrumentEntry,
    _plan,
    _registered,
    _resolve,
    _resolve_point,
    _Resolved,
    _satisfies,
    _target_version,
    _trigger_specifiers,
)

_USAGE = """\
usage: python -m wrapture.tools instrumentation [--config PATH] [--verbose]
       python -m wrapture.tools instrumentation --toml [--enabled] [--config PATH]

List the instrumentation installed in this environment, from class
data alone, applying nothing: each registered instrumentation with
its distribution and version, description, target and installed
version with the support verdict, trigger modules, requirements,
settings with defaults, and removability. A name two distributions
register is shown qualified as name@distribution, the spelling an
[[instrument]] entry then needs.

  --config PATH   also list the reference-form (local) entries of the
                  config file, with its pythonpath applied, and mark
                  which entries the file selects
  --verbose, -v   show what applying each one here would register,
                  trigger by trigger, and the distribution's URL
  --toml          write [[instrument]] entries to standard output
                  instead of the listing, one per installed
                  instrumentation, every entry disabled and every
                  setting a commented-out line at its default; with
                  --config, only the entries the file lacks
  --enabled       with --toml, emit the entries live (no enabled = false)
"""


def _usage_error(message: str) -> NoReturn:
    print(f"wrapture: {message}", file=sys.stderr)
    print(_USAGE, file=sys.stderr, end="")
    raise SystemExit(2)


def _fail(message: str) -> NoReturn:
    print(f"wrapture: {message}", file=sys.stderr)
    raise SystemExit(1)


@dataclass(frozen=True)
class _Options:
    config: str | None
    verbose: bool
    toml: bool
    enabled: bool


def _parse(argv: Sequence[str]) -> _Options:
    """Parse an instrumentation command line."""

    config: str | None = None
    verbose = False
    toml = False
    enabled = False
    pending = list(argv)

    while pending:
        argument = pending[0]

        if argument in ("-h", "--help"):
            print(_USAGE, end="")
            raise SystemExit(0)

        if argument == "--config":
            if len(pending) < 2:
                _usage_error("--config requires a value")
            config = pending[1]
            del pending[:2]
            continue

        if argument.startswith("--config="):
            config = argument.partition("=")[2]
            del pending[0]
            continue

        if argument in ("-v", "--verbose"):
            verbose = True
            del pending[0]
            continue

        if argument == "--toml":
            toml = True
            del pending[0]
            continue

        if argument == "--enabled":
            enabled = True
            del pending[0]
            continue

        if argument.startswith("-"):
            _usage_error(f"unknown option {argument!r}")

        _usage_error(f"unexpected argument {argument!r}")

    if enabled and not toml:
        _usage_error("--enabled only applies with --toml")

    return _Options(config, verbose, toml, enabled)


# ---------------------------------------------------------------------------
# gathering
# ---------------------------------------------------------------------------


@dataclass
class _Listed:
    # One instrumentation as the listing sees it: resolved (or not),
    # where it came from, and what the config file says about it.

    spec: str
    resolved: _Resolved | None = None
    error: str | None = None
    point: metadata.EntryPoint | None = None
    imported: list[str] = field(default_factory=list)
    selected: str | None = None

    @property
    def local(self) -> bool:
        return self.point is None

    @property
    def name(self) -> str:
        if self.resolved is not None:
            return self.resolved.name
        if self.point is not None:
            return self.point.name
        return self.spec

    @property
    def distribution(self) -> str | None:
        if self.resolved is not None:
            return self.resolved.distribution
        if self.point is not None:
            dist = getattr(self.point, "dist", None)
            return getattr(dist, "name", None) if dist is not None else None
        return None

    @property
    def version(self) -> str:
        if self.resolved is not None:
            return self.resolved.version
        if self.point is not None:
            dist = getattr(self.point, "dist", None)
            return str(getattr(dist, "version", "") or "") if dist is not None else ""
        return ""


def _loading(load: Any) -> tuple[_Resolved | None, list[str], str | None]:
    # Run one resolution with the import-safety warning silenced: the
    # resolution carries the offending names as data, which the
    # listing shows in place rather than as a warning on stderr.

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConfigWarning)
        try:
            resolved: _Resolved = load()
        except ConfigError as exc:
            return None, [], str(exc)

    return resolved, list(resolved.imported), None


def _installed() -> list[_Listed]:
    """Every instrumentation registered in the entry point group,
    loaded and described, in name order; a name several distributions
    register is spelt qualified."""

    points = _registered()
    counts: dict[str, int] = {}
    for point in points:
        counts[point.name] = counts.get(point.name, 0) + 1

    listed: list[_Listed] = []
    for point in points:
        dist = getattr(point, "dist", None)
        distribution = getattr(dist, "name", None) if dist is not None else None
        spec = (
            f"{point.name}@{distribution}"
            if counts[point.name] > 1 and distribution
            else point.name
        )

        where = f"instrumentation {spec!r}"
        resolved, imported, error = _loading(
            lambda point=point, where=where: _resolve_point(point, where=where)
        )
        listed.append(_Listed(spec, resolved, error, point, imported))

    listed.sort(key=lambda item: (item.name, item.distribution or ""))
    return listed


def _from_config(path: str, installed: list[_Listed]) -> list[_Listed]:
    """Read a config file's [[instrument]] entries: mark the installed
    ones it selects, and return the reference-form (local) ones as
    listings of their own."""

    document, location = _read_document(path)
    if not isinstance(document, dict):
        raise ConfigError(f"{location}: config must be a TOML table")

    _apply_pythonpath(document, location)
    entries: list[InstrumentEntry] = _instrument_entries(document)

    local: list[_Listed] = []
    for entry in entries:
        state = "enabled" if entry.enabled else "disabled"
        spec = entry.label

        if ":" in spec:
            resolved, imported, error = _loading(
                lambda spec=spec: _resolve(spec, where=f"instrument entry {spec!r}")
            )
            local.append(_Listed(spec, resolved, error, None, imported, state))
            continue

        # A registered name: find it among the installed listing by the
        # same rule the config uses, bare or qualified.

        resolved, _, error = _loading(
            lambda spec=spec: _resolve(spec, where=f"instrument entry {spec!r}")
        )
        if resolved is None:
            local.append(_Listed(spec, None, error, None, [], state))
            continue

        for item in installed:
            if item.resolved is not None and item.resolved.cls is resolved.cls:
                item.selected = state
                break

    return local


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _target_line(resolved: _Resolved) -> str:
    # "flask 3.1.0, supported (>=2.0,<4)", "flask 1.0, outside >=2.0,<4",
    # "flask, not installed", or just "flask 3.1.0".

    cls = resolved.cls
    version = _target_version(cls.target)

    if version is None:
        text = f"{cls.target}, not installed"
        if cls.supports:
            text += f" (supports {cls.supports})"
        return text

    text = f"{cls.target} {version}"
    if cls.supports:
        verdict = _satisfies(cls.supports, version)
        if verdict:
            text += f", supported ({cls.supports})"
        elif verdict is None:
            text += f", version unparseable against {cls.supports}"
        else:
            text += f", outside {cls.supports}"

    return text


def _removable_text(cls: type[Instrumentation]) -> str:
    # The effective claim per trigger: one word when uniform, and the
    # split spelled out when a hook's removable= differs from the rest.

    verdicts = {
        name: (hook.removable if hook.removable is not None else cls.removable)
        for name, hook in cls._hooks.items()
    }

    if all(verdicts.values()):
        return "yes"
    if not any(verdicts.values()):
        return "no"

    yes = [name for name, verdict in verdicts.items() if verdict]
    no = [name for name, verdict in verdicts.items() if not verdict]
    return f"{', '.join(yes)} only, not {', '.join(no)}"


def _modules_text(resolved: _Resolved) -> str:
    parts = []
    for name, specifier in _trigger_specifiers(resolved.cls).items():
        parts.append(f"{name} ({specifier})" if specifier else name)
    return ", ".join(parts) if parts else "(none)"


def _project_url(point: metadata.EntryPoint | None) -> str | None:
    if point is None:
        return None
    dist = getattr(point, "dist", None)
    if dist is None:
        return None

    try:
        meta = dist.metadata
    except Exception:
        return None

    home = meta.get("Home-page")
    if home:
        return str(home)

    urls = meta.get_all("Project-URL") or []
    for entry in urls:
        label, _, url = str(entry).partition(",")
        if label.strip().lower() in ("homepage", "home", "repository", "source"):
            return url.strip()
    if urls:
        return str(urls[0]).partition(",")[2].strip()

    return None


def _render_listing(
    items: Sequence[_Listed], *, verbose: bool, config: str | None
) -> str:
    """The human-readable listing, one block per instrumentation."""

    if not items:
        return "no instrumentation is installed or named by the config\n"

    blocks: list[str] = []
    for item in items:
        lines: list[str] = []

        if item.local:
            source = f"(local: {item.spec})"
            title = item.name if item.resolved is not None else item.spec
        else:
            source = f"({item.distribution} {item.version})".replace(" )", ")")
            title = item.spec
        lines.append(f"{title}  {source}")

        if item.error is not None:
            lines.append(f"  error: {item.error}")
            if item.selected and config:
                lines.append(f"  config: {item.selected} in {config}")
            blocks.append("\n".join(lines))
            continue

        resolved = item.resolved
        assert resolved is not None
        cls = resolved.cls

        if resolved.description:
            lines.append(f"  {resolved.description}")
        lines.append(f"  target: {_target_line(resolved)}")
        lines.append(f"  modules: {_modules_text(resolved)}")
        if cls.requires:
            lines.append(f"  requires: {', '.join(cls.requires)}")
        lines.append(f"  removable: {_removable_text(cls)}")

        if cls.settings:
            lines.append("  settings:")
            rows = [
                (f"{name} = {_toml_value(setting.default)}", setting.description)
                for name, setting in cls.settings.items()
            ]
            width = max(len(text) for text, _ in rows)
            for text, description in rows:
                tail = f"   {description}" if description else ""
                lines.append(f"    {text.ljust(width) if tail else text}{tail}")
        else:
            lines.append("  settings: (none)")

        if item.selected and config:
            lines.append(f"  config: {item.selected} in {config}")

        if item.imported:
            lines.append(
                f"  warning: loading the class imported {', '.join(item.imported)},"
                f" which it claims as its target or triggers; the class's"
                f" module should import only wrapture"
            )

        if verbose:
            for plan in _plan(cls, _target_version(cls.target)):
                if plan.skipped is None:
                    lines.append(f"  would register: {plan.name}")
                else:
                    lines.append(f"  would skip: {plan.name} ({plan.skipped})")
            url = _project_url(item.point)
            if url:
                lines.append(f"  url: {url}")

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + "\n"


def _toml_key(key: str) -> str:
    if key and all(ch.isalnum() or ch in "-_" for ch in key):
        return key
    return json.dumps(key)


def _toml_value(value: Any) -> str:
    """Render a setting default as a TOML value. None has no TOML
    spelling and renders as a placeholder the caller comments out."""

    if value is None:
        return "..."
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, Mapping):
        inner = ", ".join(
            f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items()
        )
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value))


def _render_toml(items: Sequence[_Listed], *, enabled: bool, skip: set[str]) -> str:
    """[[instrument]] entries for the installed instrumentation, as a
    template: a three-line comment header, the name (qualified only
    when two distributions register it), enabled = false unless live
    output was asked for, and every setting commented out at its
    default with its description beside it."""

    installed = [item for item in items if not item.local]

    by_name: dict[str, list[_Listed]] = {}
    by_target: dict[str, list[_Listed]] = {}
    for item in installed:
        by_name.setdefault(item.name, []).append(item)
        if item.resolved is not None:
            by_target.setdefault(item.resolved.cls.target, []).append(item)

    blocks: list[str] = []
    for item in installed:
        if item.spec in skip:
            continue

        qualified = (
            f"{item.name}@{item.distribution}" if item.distribution else item.name
        )
        lines: list[str] = []

        if item.resolved is None:
            lines.append(f"# {qualified}: could not be loaded: {item.error}")
            blocks.append("\n".join(lines))
            continue

        resolved = item.resolved
        cls = resolved.cls

        header = f"# {qualified} {resolved.version}".rstrip()
        lines.append(header)
        if resolved.description:
            lines.append(f"# {resolved.description}")

        notes = [f"target {_target_line(resolved)}"]
        if cls.requires:
            notes.append(f"requires {', '.join(cls.requires)}")

        others = [
            other.distribution
            for other in by_name.get(item.name, [])
            if other is not item and other.distribution
        ]
        if others:
            notes.append(f"also provided by {', '.join(others)}, enable one")

        rivals = [
            other.spec
            for other in by_target.get(cls.target, [])
            if other is not item and other.name != item.name
        ]
        if rivals:
            notes.append(
                f"conflicts with {', '.join(rivals)} (same target), enable one"
            )

        lines.append(f"# {'; '.join(notes)}")

        lines.append("[[instrument]]")
        lines.append(f"name = {json.dumps(item.spec)}")
        if not enabled:
            lines.append("enabled = false")

        if cls.settings:
            rows = []
            for name, setting in cls.settings.items():
                text = f"# {name} = {_toml_value(setting.default)}"
                note = setting.description
                if setting.default is None:
                    note = f"(no default) {note}".strip()
                rows.append((text, note))
            width = max(len(text) for text, _ in rows)
            for text, note in rows:
                if note:
                    lines.append(f"{text.ljust(width)}   # {note}")
                else:
                    lines.append(text)

        blocks.append("\n".join(lines))

    if not blocks:
        return "# no instrumentation to add\n"

    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------


def main(argv: Sequence[str]) -> None:
    """Run the instrumentation command over the given arguments."""

    options = _parse(argv)

    installed = _installed()

    local: list[_Listed] = []
    if options.config is not None:
        try:
            local = _from_config(options.config, installed)
        except ConfigError as exc:
            _fail(str(exc))

    if options.toml:
        # With a config, only what the file lacks: the installed entries
        # it already selects, enabled or not, are left out.

        selected = {item.spec for item in installed if item.selected is not None}
        sys.stdout.write(
            _render_toml(installed, enabled=options.enabled, skip=selected)
        )
        return

    sys.stdout.write(
        _render_listing(
            [*installed, *local], verbose=options.verbose, config=options.config
        )
    )
