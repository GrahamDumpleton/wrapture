"""Entry point for the wrapture command line tools.

python -m wrapture.tools lists the available commands;
python -m wrapture.tools COMMAND runs one.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from . import convert, instrumentation

_USAGE = """\
usage: python -m wrapture.tools COMMAND [options]

Command line tools over recorded traces and the environment. Commands:

  convert          render a JSONLines trace file in another format:
                   chrome (Perfetto timeline), mermaid (sequence
                   diagram), canonical (snapshot text tree)
  instrumentation  list the instrumentation installed in this
                   environment, or write [[instrument]] entries for it

python -m wrapture.tools COMMAND -h shows each command's options.
"""


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch to a tools command, or list the commands."""

    arguments = list(sys.argv[1:] if argv is None else argv)

    if not arguments or arguments[0] in ("-h", "--help"):
        print(_USAGE, end="")
        raise SystemExit(0)

    command, rest = arguments[0], arguments[1:]

    if command == "convert":
        convert.main(rest)
        return

    if command == "instrumentation":
        instrumentation.main(rest)
        return

    print(
        f"wrapture: unknown command {command!r}: expected convert or instrumentation",
        file=sys.stderr,
    )
    print(_USAGE, file=sys.stderr, end="")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
