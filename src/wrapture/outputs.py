"""Templated output paths.

Every path wrapture writes to (a JSONLines trace, a Printer file, later a
window's report) is a template, expanded when the file is opened and
never per line. A path with no variables is a template that expands to
itself, so there is one code path. Variables:

    {pid}            process id
    {host}           hostname
    {name}           the sink's or window's name
    {date}           2026-08-18 (local time)
    {time}           14-05-30 (local time, filesystem-safe)
    {datetime}       2026-08-18T14-05-30
    {epoch}          1755525930 (integer seconds)
    {now:%Y%m%d-%H}  strftime in local time, any format
    {utc:%Y%m%d-%H}  strftime in UTC
    {window}         the window's name (inside a window only)
    {first}          the window schedule's first run time (inside a window only)
    {run}            the run number within the schedule (inside a window only)

Unknown variables are an error when the template is built, never at
first rotation an hour into a run. Expanded values are sanitised so a
value can never contain a path separator or "..", and so an expansion
cannot escape the directory of the template's static prefix. Opening a
templated path always creates its parent directories.
"""

from __future__ import annotations

import os
import re
import socket
import string
import time
from collections.abc import Mapping
from typing import Any, TextIO, cast

__all__ = ["OutputPath", "open_output"]


_now = time.time

_SIMPLE = frozenset({"pid", "host", "name", "date", "time", "datetime", "epoch"})
_FORMATTED = frozenset({"now", "utc"})
_WINDOWED = frozenset({"window", "first", "run"})
_TIMED = frozenset({"date", "time", "datetime", "epoch", "now", "utc"})

_UNSAFE = re.compile(r"[\\/\0]")


def _sanitise(value: str) -> str:
    # A variable's value is one path component at most: separators
    # become dashes and a run of dots that could climb a directory is
    # flattened, so nothing an expansion produces can leave the
    # directory the static part of the template names.

    value = _UNSAFE.sub("-", value)
    if value in ("", ".", ".."):
        return "_"
    return value.replace("..", "_")


class OutputPath:
    """A templated output path, expanded at open time.

    `template` is a path with optional {variable} fields; `name` is the
    value {name} expands to. Building one validates the template, so a
    misspelt variable fails where the path is given rather than when
    the file is first opened. The window variables ({window}, {first},
    {run}) take their values from `context`, which the window holding
    the sink sets for each run; expanding them with no context is an
    error, since outside a window there is nothing for them to name.
    """

    def __init__(self, template: str | os.PathLike[str], *, name: str) -> None:
        self._template = os.fspath(template)
        self._name = name
        self._variables: set[str] = set()
        self.context: Mapping[str, Any] | None = None

        self._validate()

    def _validate(self) -> None:
        for _, field, spec, conversion in string.Formatter().parse(self._template):
            if field is None:
                continue

            if conversion is not None:
                raise ValueError(
                    f"output path {self._template!r}: conversions such as"
                    f" !r are not supported in {{{field}!{conversion}}}"
                )

            if field in _FORMATTED:
                if not spec:
                    raise ValueError(
                        f"output path {self._template!r}: {{{field}}} needs a"
                        f" strftime format, as in {{{field}:%Y%m%d-%H}}"
                    )
            elif field == "run":
                if spec and not re.fullmatch(r"0?\d*", spec):
                    raise ValueError(
                        f"output path {self._template!r}: {{run}} takes an"
                        f" integer width such as {{run:02}}, not {spec!r}"
                    )
            elif field in _SIMPLE or field in _WINDOWED:
                if spec:
                    raise ValueError(
                        f"output path {self._template!r}: {{{field}}} takes"
                        f" no format specification"
                    )
            else:
                known = sorted(_SIMPLE | _FORMATTED | _WINDOWED)
                raise ValueError(
                    f"output path {self._template!r}: unknown variable"
                    f" {{{field}}}; the variables are {known}"
                )

            self._variables.add(field)

    def __repr__(self) -> str:
        return f"OutputPath({self._template!r})"

    def __str__(self) -> str:
        return self._template

    @property
    def template(self) -> str:
        return self._template

    @property
    def variables(self) -> frozenset[str]:
        """The variable names the template uses."""

        return frozenset(self._variables)

    @property
    def windowed(self) -> bool:
        """Whether the template uses a window variable, so that it names
        a different file per run of the window holding it."""

        return bool(self._variables & _WINDOWED)

    @property
    def timed(self) -> bool:
        """Whether the template uses a time variable, so that expanding
        it again later can name a different file."""

        return bool(self._variables & _TIMED)

    def expand(
        self, *, when: float | None = None, window: Mapping[str, Any] | None = None
    ) -> str:
        """The concrete path the template names now (or at `when`, an
        epoch time), with the window variables taken from `window`."""

        moment = _now() if when is None else when
        local = time.localtime(moment)
        utc = time.gmtime(moment)

        if window is None:
            window = self.context

        if window is None and self.windowed:
            missing = sorted(self._variables & _WINDOWED)
            raise ValueError(
                f"output path {self._template!r}: {missing} only have a value"
                f" inside a window"
            )

        values: dict[str, Any] = {}

        for field in self._variables:
            if field == "pid":
                values[field] = str(os.getpid())
            elif field == "host":
                values[field] = _sanitise(socket.gethostname())
            elif field == "name":
                values[field] = _sanitise(self._name)
            elif field == "date":
                values[field] = time.strftime("%Y-%m-%d", local)
            elif field == "time":
                values[field] = time.strftime("%H-%M-%S", local)
            elif field == "datetime":
                values[field] = time.strftime("%Y-%m-%dT%H-%M-%S", local)
            elif field == "epoch":
                values[field] = str(int(moment))
            elif field == "now":
                values[field] = _Formatted(local)
            elif field == "utc":
                values[field] = _Formatted(utc)
            elif field == "run":
                values[field] = int((window or {}).get("run", 0))
            else:
                values[field] = _sanitise(str((window or {}).get(field, "")))

        return self._template.format(**values)


def open_output(path: str, mode: str = "a") -> TextIO:
    """Open an expanded output path for UTF-8 text, creating its parent
    directories first."""

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    return cast(TextIO, open(path, mode, encoding="utf-8"))


class _Formatted:
    # The value behind {now:...} and {utc:...}: the format spec is a
    # strftime format, applied and sanitised when str.format asks.

    def __init__(self, moment: time.struct_time) -> None:
        self._moment = moment

    def __format__(self, spec: str) -> str:
        return _sanitise(time.strftime(spec, self._moment))
