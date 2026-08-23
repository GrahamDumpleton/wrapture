"""Run a Python program with a wrapture config applied first.

    python -m wrapture [--config PATH] (-m MODULE | SCRIPT) [ARGS...]

The config is resolved, loaded and applied before the target runs, so
patches land before the target module imports anything: the same
ordering guarantee the injection path gives. The target then runs as
__main__ with sys.argv rebuilt to the target and its arguments,
exactly as python itself would have run it.

This module is the runner's entry point only, the same private -m
convention as pdb, cProfile and coverage: run it, do not import it.
"""

from __future__ import annotations

import os
import runpy
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn

from .config import find_config, load_config
from .exceptions import ConfigError

_USAGE = """\
usage: python -m wrapture [--config PATH] (-m MODULE | SCRIPT) [ARGS...]

Apply a wrapture config, then run the target as __main__ with ARGS as
its arguments. With --config the named TOML file is used; without it
the standard precedence chain locates one: a path in WRAPTURE_CONFIG,
wrapture.toml in the current directory, then a [tool.wrapture] table
in pyproject.toml. Everything after the target belongs to the target,
so wrapture's own options must come before it.

Trace files a run produces can be rendered in other formats with
python -m wrapture.tools convert.
"""


@dataclass(frozen=True)
class _Invocation:
    config: str | None
    module: str | None
    script: str | None
    arguments: tuple[str, ...]


def _usage_error(message: str) -> NoReturn:
    print(f"wrapture: {message}", file=sys.stderr)
    print(_USAGE, file=sys.stderr, end="")
    raise SystemExit(2)


def _parse(argv: Sequence[str]) -> _Invocation:
    """Split the command line into wrapture's own options and the
    target with its arguments.

    Parsing stops at the first target: everything after -m MODULE or
    the script path is the target's own, however option-like it
    looks, matching how python itself treats a command line.
    """

    config: str | None = None
    pending = list(argv)

    while pending:
        argument = pending[0]

        if argument in ("-h", "--help"):
            print(_USAGE, end="")
            raise SystemExit(0)

        if argument == "--config":
            if len(pending) < 2:
                _usage_error("--config requires a path")
            config = pending[1]
            del pending[:2]
            continue

        if argument.startswith("--config="):
            config = argument.partition("=")[2]
            if not config:
                _usage_error("--config requires a path")
            del pending[0]
            continue

        if argument == "-m":
            if len(pending) < 2:
                _usage_error("-m requires a module name")
            return _Invocation(config, pending[1], None, tuple(pending[2:]))

        if argument.startswith("-"):
            _usage_error(f"unknown option {argument!r}")

        return _Invocation(config, None, argument, tuple(pending[1:]))

    _usage_error("a target is required: -m MODULE or a script path")


def main(argv: Sequence[str] | None = None) -> None:
    """Parse the command line, apply the config, and run the target."""

    invocation = _parse(sys.argv[1:] if argv is None else argv)

    # Resolve and apply the config before anything else happens, so
    # patches and instrumentation are in place before the target module
    # imports anything. Finding no config is an error rather than a
    # silent untraced run.

    source = invocation.config if invocation.config is not None else find_config()

    if source is None:
        print(
            "wrapture: no config found: pass --config PATH, set"
            " WRAPTURE_CONFIG, or provide wrapture.toml or a"
            " [tool.wrapture] table in pyproject.toml in the current"
            " directory",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        load_config(source).apply()
    except ConfigError as exc:
        print(f"wrapture: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    # Hand over to the target as python itself would have run it. For
    # a module target, alter_sys replaces sys.argv[0] with the
    # module's own file, matching python -m; for a script, its
    # directory joins the front of sys.path, matching python script.py.

    if invocation.module is not None:
        sys.argv[:] = [invocation.module, *invocation.arguments]
        runpy.run_module(invocation.module, run_name="__main__", alter_sys=True)
    else:
        script = invocation.script
        assert script is not None

        sys.argv[:] = [script, *invocation.arguments]
        sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
        runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
