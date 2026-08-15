# All supported Python versions, including free threaded (t) builds.
# 3.15 is in RC release phase but is expected to work.
python_versions := "3.12 3.13 3.13t 3.14 3.14t 3.15 3.15t"

# List available targets.
default:
    @just --list

# Run the test suite on the default Python version; extra args go to pytest.
test *ARGS:
    uv run pytest {{ARGS}}

# Extra arguments are passed through to pytest. Each version gets its own
# environment so the default .venv is untouched, and only the test
# dependency group is installed: dev tools such as mypy do not build on
# the free threaded versions and are not needed to run tests.
# Run the test suite on one nominated version, e.g. `just test-python 3.13t`.
test-python VERSION *ARGS:
    UV_PROJECT_ENVIRONMENT=.venv-{{VERSION}} uv run --python {{VERSION}} --no-default-groups --group test pytest {{ARGS}}

# Run the test suite on every supported Python version.
test-all *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    for version in {{python_versions}}; do
        echo "=== Python ${version} ==="
        just test-python "${version}" {{ARGS}}
    done

# Check code with the ruff linter and formatter.
lint:
    uv run ruff check src tests
    uv run ruff format --check src tests

# Reformat code and fix lint issues that are auto-fixable.
format:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# Type check the project with mypy.
typecheck:
    uv run mypy

# Build the documentation into docs/_build/html.
docs:
    uv run --extra docs sphinx-build -W docs docs/_build/html

# Build the documentation if out of date, then open it in the browser.
docs-open: docs
    open docs/_build/html/index.html

# An incremental Sphinx build can carry stale state across structural
# changes such as renamed or removed pages.
# Remove the built documentation, forcing the next build to start fresh.
docs-clean:
    rm -rf docs/_build

# Remove temporary files: caches, virtual environments and build artifacts.
clean: docs-clean
    rm -rf .venv .venv-*
    rm -rf build dist src/*.egg-info *.egg-info
    rm -rf .pytest_cache .mypy_cache .ruff_cache
    find . -type d -name __pycache__ -not -path "./scratch/*" -exec rm -rf {} +
