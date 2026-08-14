# Agent guidance for wrapture

## Project

wrapture is a Python library for attaching bindings to arbitrary Python call
sites, without modifying the code being observed, for use in monkey patching,
testing, tracing and profiling. It builds on wrapt (2.4.0+). See README.md
for the project goals.

The package uses a src layout: the code lives in src/wrapture/.

Tests live in the tests/ directory. See TESTING.md for where tests are, how
to run them, and conventions for adding new ones.

The scratch/ directory is not part of the git repo. It holds temporary
working files, such as reference material given to an agent or plans an
agent is asked to generate. Its contents come and go, so never reference
scratch/ files by name from code or documentation that will be committed.

## Tooling: always use uv

All Python environment and package management in this project is done with
[uv](https://docs.astral.sh/uv/). Never use the Python venv module, bare
pip, or python -m build directly.

- Run commands in the project environment: `uv run <command>`
  (e.g. `uv run pytest`)
- Run a Python interpreter: `uv run python`
- Build sdist and wheel: `uv build`
- Add or remove dependencies (updates pyproject.toml): `uv add <package>`,
  `uv remove <package>`
- Sync the environment from pyproject.toml: `uv sync`

## Style

- Do not use emdashes in any files in this project. Rephrase with commas,
  parentheses, colons, or separate sentences instead.
- Project code must always use Python type hints. Add them to all function
  and method signatures (parameters and return types), and to attributes
  and variables where the type is not obvious from the assignment. When
  adding or modifying code that lacks type hints, add them.

## Git

- Git commit messages must never include a co-authored-by agent message or
  any similar agent attribution trailer.
