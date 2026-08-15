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
- Use vertical white space liberally inside function and method bodies.
  Write code in paragraphs: group the statements that together perform one
  step, and separate each group from the next with a blank line. Natural
  paragraph boundaries include setup versus the main work versus the
  result, before and after a conditional or loop, and around a with or
  try block. Do not cram a body into one contiguous blob, and equally do
  not put a blank line between every single statement; the blank lines
  should mark where one thought ends and the next begins.
- Where it helps the reader, start a paragraph of code with a short
  comment saying what that step does or why it is needed. Prefer one
  comment per logical block over line-by-line commentary, and skip the
  comment entirely when the code already says it plainly.
- Put a blank line between such a block comment and the code below it:
  the comment introduces the paragraph rather than sitting flush against
  its first line.
- Put a blank line between a function or method docstring and the first
  line of code in the body.
- Every function, method or property that is part of the public API must
  have a docstring saying what it does. The exceptions are cases that are
  truly trivial and obvious, such as an accessor property named for the
  attribute it returns, and dunder methods implementing standard
  protocols.

## Git

- Git commit messages must never include a co-authored-by agent message or
  any similar agent attribution trailer.
