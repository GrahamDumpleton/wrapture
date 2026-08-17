"""The convert command: render a JSONLines trace file in another
format.

    python -m wrapture.tools convert --format FORMAT [-o OUTPUT] TRACE

A thin shell over load_events() and the exporters, so the runner's
trace file reaches Perfetto, a pull request comment, or a snapshot
file without writing any code.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from ..export import canonical, chrome_trace, load_events, mermaid

_USAGE = """\
usage: python -m wrapture.tools convert --format FORMAT [-o OUTPUT] TRACE

Render the JSONLines trace file TRACE in another format, written to
OUTPUT, or to standard output with no -o so a rendering can be piped
onwards. Formats:

  chrome      Chrome trace JSON; open in the Perfetto UI
              (https://ui.perfetto.dev) for a per-thread timeline
  mermaid     sequence diagram; renders on GitHub and in most
              documentation tooling
  canonical   deterministic text tree for snapshot comparisons
"""

_FORMATS: dict[str, Callable[[Any], str]] = {
    "chrome": chrome_trace,
    "mermaid": mermaid,
    "canonical": canonical,
}


def _usage_error(message: str) -> NoReturn:
    print(f"wrapture: {message}", file=sys.stderr)
    print(_USAGE, file=sys.stderr, end="")
    raise SystemExit(2)


def _fail(message: str) -> NoReturn:
    print(f"wrapture: {message}", file=sys.stderr)
    raise SystemExit(1)


@dataclass(frozen=True)
class _Conversion:
    format: str
    output: str | None
    source: str


def _parse(argv: Sequence[str]) -> _Conversion:
    """Parse a convert command line: the format, an optional output
    path, and exactly one trace file, accepted in any order."""

    format_name: str | None = None
    output: str | None = None
    source: str | None = None
    pending = list(argv)

    def take_value(option: str) -> str:
        if len(pending) < 2:
            _usage_error(f"{option} requires a value")
        value = pending[1]
        del pending[:2]
        return value

    while pending:
        argument = pending[0]

        if argument in ("-h", "--help"):
            print(_USAGE, end="")
            raise SystemExit(0)

        if argument == "--format":
            format_name = take_value("--format")
            continue

        if argument.startswith("--format="):
            format_name = argument.partition("=")[2]
            del pending[0]
            continue

        if argument in ("-o", "--output"):
            output = take_value(argument)
            continue

        if argument.startswith("--output="):
            output = argument.partition("=")[2]
            del pending[0]
            continue

        if argument.startswith("-") and argument != "-":
            _usage_error(f"unknown option {argument!r}")

        if source is not None:
            _usage_error(f"unexpected argument {argument!r}")
        source = argument
        del pending[0]

    if format_name is None:
        _usage_error("convert requires --format")

    if format_name not in _FORMATS:
        _usage_error(
            f"unknown format {format_name!r}: expected one of"
            f" {', '.join(sorted(_FORMATS))}"
        )

    if source is None:
        _usage_error("convert requires a trace file")

    return _Conversion(format_name, output, source)


def main(argv: Sequence[str]) -> None:
    """Run the convert command over the given arguments."""

    conversion = _parse(argv)

    try:
        records = load_events(conversion.source)
    except OSError as exc:
        _fail(f"cannot read trace file {conversion.source}: {exc}")
    except ValueError as exc:
        _fail(f"{conversion.source} is not a JSONLines trace: {exc}")

    # Every rendering ends with a newline, so output composes with
    # shells and diff tools whichever format was chosen.

    rendered = _FORMATS[conversion.format](records)
    if not rendered.endswith("\n"):
        rendered += "\n"

    if conversion.output is None:
        sys.stdout.write(rendered)
        return

    try:
        with open(conversion.output, "w", encoding="utf-8") as stream:
            stream.write(rendered)
    except OSError as exc:
        _fail(f"cannot write {conversion.output}: {exc}")
