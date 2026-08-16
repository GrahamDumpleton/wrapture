# Testing

## Where the tests are

Tests live in the [tests/](tests/) directory at the top of the repository,
separate from the package code in src/wrapture/. Test files are named
`test_*.py` and are discovered by pytest, which is configured via the
`[tool.pytest.ini_options]` section of [pyproject.toml](pyproject.toml).

The docs/ directory is also on the test paths, with doctest collection
enabled for markdown files: the interpreter transcripts in the getting
started page run as doctests on every test run, so the examples in the
documentation cannot silently rot.

## Running the tests

All tooling in this project goes through [uv](https://docs.astral.sh/uv/),
which manages the project environment and installs the package and its
development dependencies (including pytest) automatically.

The simplest way to run the test suite is via the Justfile target:

```console
just test
```

Extra arguments are passed through to pytest, for example:

```console
just test -v
just test tests/test_version.py
just test -k version
```

Equivalently, run pytest directly with uv:

```console
uv run pytest
```

## Testing across Python versions

The project supports multiple Python versions, including the free threaded
builds of 3.13, 3.14 and 3.15. The supported list is defined at the top of
the [Justfile](Justfile). The default version used by plain `just test` is
pinned in [.python-version](.python-version).

Run the test suite on every supported version:

```console
just test-all
```

Run the test suite on one nominated version:

```console
just test-python 3.12
just test-python 3.14t
```

Extra arguments are passed through to pytest for these targets too. uv
downloads any Python version it does not already have, and each version
gets its own environment (.venv-VERSION) so the default .venv is left
untouched.

## Writing tests

- Put new test files in tests/ and name them `test_*.py`.
- Import the package under test as `wrapture`. The project is installed
  into the uv-managed environment, so no path manipulation is needed.
- Tests should not depend on anything in the scratch/ directory, which is
  not part of the repository.
