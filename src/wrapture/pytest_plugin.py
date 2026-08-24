"""An opt-in pytest plugin for suites that use wrapture.

Deliberately not auto-loaded: activate it from a conftest.py

    pytest_plugins = ["wrapture.pytest_plugin"]

or on the command line with `-p wrapture.pytest_plugin`. Activated, it
provides:

- A sweep after every test for bindings the test applied and did not
  remove. Leaked bindings are removed, so later tests are not poisoned,
  and the test fails naming them. Bindings applied by wider-scoped
  fixtures before the test began are not the test's leaks and are left
  alone.
- A `tape` fixture: a recording scope spanning the test, yielding the
  Tape. When a test that used it fails, the tape's call tree is
  attached to the failure report.
- Assertion output for comparisons involving an EventLog, printing the
  events, with the discarded events of an over-narrowed filter chain
  shown the way the assert_* methods show them.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from typing import Any

import pytest

from .bindings import _applied_bindings
from .eventlogs import EventLog
from .timeline import Tape, timeline

_TAPE_KEY = pytest.StashKey[Tape]()


@pytest.fixture
def tape(request: pytest.FixtureRequest) -> Iterator[Tape]:
    """A recording scope spanning the whole test.

    Bindings the test applies, however it applies them, record onto
    it. Because this timeline is given no bindings, it applies none and
    verifies no declared expectations; use timeline(...) inside the
    test when those are wanted. When a test that used this fixture
    fails, the tape's tree is attached to the failure report.
    """

    with timeline() as recording:
        request.node.stash[_TAPE_KEY] = recording
        yield recording


@pytest.fixture(autouse=True)
def _wrapture_leak_sweep() -> Iterator[None]:
    # Snapshot what was already applied before the test's own fixtures
    # and body ran: those belong to wider scopes and are not this
    # test's leaks. This fixture is autouse and function scoped, so its
    # teardown runs after the test's own fixtures have torn down.

    before = {bnd for bnd in _applied_bindings if bnd.applied}

    yield

    leaked = [bnd for bnd in _applied_bindings if bnd.applied and bnd not in before]

    if leaked:
        labels = ", ".join(sorted(bnd.label or bnd.path for bnd in leaked))

        for bnd in leaked:
            bnd.remove()

        pytest.fail(
            f"wrapture: bindings left applied after the test: {labels}"
            f" (removed now, so later tests are unaffected)",
            pytrace=False,
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Any, Any]:
    report = yield

    # Attach the tape's tree to a failing test that used the tape
    # fixture, so the failure output shows what actually ran.

    if (
        isinstance(report, pytest.TestReport)
        and report.when == "call"
        and report.failed
    ):
        recording = item.stash.get(_TAPE_KEY, None)
        if recording is not None and recording.all:
            report.sections.append(("wrapture tape", recording.tree()))

    return report


def pytest_assertrepr_compare(
    config: pytest.Config, op: str, left: Any, right: Any
) -> list[str] | None:
    """Explain comparisons involving an EventLog with the events shown."""

    if not isinstance(left, EventLog) and not isinstance(right, EventLog):
        return None

    def shorthand(value: Any) -> str:
        if isinstance(value, EventLog):
            return f"<EventLog {value.label}: {value.count} event(s)>"
        return repr(value)

    lines = [f"{shorthand(left)} {op} {shorthand(right)}"]

    for side in (left, right):
        if isinstance(side, EventLog):
            lines.extend(side._describe())

    return lines
