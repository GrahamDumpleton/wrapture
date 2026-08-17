"""Tests for stack capture: depths, interning, elision, and readback.

Stack capture is per binding and off by default. When on, each recorded
event carries an interned integer id, resolved back to frames with
stack_frames(), innermost first, with the observation machinery's own
frames elided so the stack starts at the code under observation.
"""

from typing import Any

import pytest

from wrapture import binding, stack_frames, timeline
from wrapture.stacks import _stacks


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


class Model:
    @property
    def total(self) -> int:
        return 42


def place_order(gateway: Gateway) -> str:
    return gateway.charge(500)


# ---------------------------------------------------------------------------
# depths
# ---------------------------------------------------------------------------


def test_no_capture_by_default() -> None:
    charge = binding(Gateway, "charge")

    with timeline(charge):
        Gateway().charge(500)

        assert charge.events.first.stack is None


def test_caller_captures_exactly_the_calling_frame() -> None:
    charge = binding(Gateway, "charge", stack="caller")

    with timeline(charge):
        place_order(Gateway())

        stack = charge.events.first.stack
        assert stack is not None

        (frame,) = stack_frames(stack)
        assert frame.function == "place_order"
        assert frame.filename.endswith("test_stacks.py")


def test_a_frame_count_walks_outward_from_the_caller() -> None:
    charge = binding(Gateway, "charge", stack=2)

    with timeline(charge):
        place_order(Gateway())

        stack = charge.events.first.stack
        assert stack is not None

        frames = stack_frames(stack)
        assert len(frames) == 2
        assert frames[0].function == "place_order"
        assert frames[1].function == (
            "test_a_frame_count_walks_outward_from_the_caller"
        )


def test_full_captures_more_than_caller() -> None:
    charge = binding(Gateway, "charge", stack="full")

    with timeline(charge):
        place_order(Gateway())

        stack = charge.events.first.stack
        assert stack is not None

        frames = stack_frames(stack)
        assert len(frames) > 2
        assert frames[0].function == "place_order"


def test_an_invalid_depth_is_rejected_at_creation() -> None:
    with pytest.raises(ValueError, match="stack must be"):
        binding(Gateway, "charge", stack=0)

    with pytest.raises(ValueError, match="stack must be"):
        binding(Gateway, "charge", stack="fll")


# ---------------------------------------------------------------------------
# interning
# ---------------------------------------------------------------------------


def test_repeated_captures_from_one_site_intern_to_one_stack() -> None:
    charge = binding(Gateway, "charge", stack="caller")

    with timeline(charge):
        gateway = Gateway()
        for _ in range(50):
            place_order(gateway)

        table_size = len(_stacks)
        ids = {event.stack for event in charge.events}

        assert len(ids) == 1
        assert len(_stacks) == table_size


def test_different_call_sites_intern_to_different_stacks() -> None:
    charge = binding(Gateway, "charge", stack="caller")

    with timeline(charge):
        gateway = Gateway()
        gateway.charge(1)
        gateway.charge(2)

        first, second = (event.stack for event in charge.events)

        assert first is not None and second is not None
        assert first != second
        assert stack_frames(first)[0].lineno != stack_frames(second)[0].lineno


# ---------------------------------------------------------------------------
# attribute events capture too
# ---------------------------------------------------------------------------


def test_an_attribute_read_names_the_line_that_triggered_it() -> None:
    total = binding(Model, "total", stack="caller")

    def render(model: Model) -> str:
        return f"total: {model.total}"

    with timeline(total):
        render(Model())

        stack = total.events.first.stack
        assert stack is not None
        assert stack_frames(stack)[0].function.endswith("render")


# ---------------------------------------------------------------------------
# machinery frames are elided
# ---------------------------------------------------------------------------


def test_no_wrapture_or_wrapt_frames_appear() -> None:
    import os

    import wrapt

    import wrapture

    package_dirs = (
        os.path.dirname(os.path.abspath(wrapture.__file__)),
        os.path.dirname(os.path.abspath(wrapt.__file__)),
    )

    charge = binding(Gateway, "charge", stack="full")

    with timeline(charge):
        place_order(Gateway())

        stack = charge.events.first.stack
        assert stack is not None

        for frame in stack_frames(stack):
            assert not frame.filename.startswith(package_dirs)


def test_capture_works_through_behaviour() -> None:
    def note(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        return args, kwargs

    charge = binding(Gateway, "charge", stack="caller").on_call.transforms_args(note)

    with timeline(charge):
        place_order(Gateway())

        stack = charge.events.first.stack
        assert stack is not None
        assert stack_frames(stack)[0].function == "place_order"


# ---------------------------------------------------------------------------
# the bounded table
# ---------------------------------------------------------------------------


def test_the_table_interns_an_overflow_marker_past_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fresh table with a tiny bound: uniques intern normally up to
    # the limit, and every new unique past it collapses onto the one
    # shared overflow marker, so memory stays bounded.

    from wrapture import stacks

    monkeypatch.setattr(stacks, "_ids", {})
    monkeypatch.setattr(stacks, "_stacks", [])
    monkeypatch.setattr(stacks, "_STACK_LIMIT", 2)

    def frames(line: int) -> tuple[stacks.StackFrame, ...]:
        return (stacks.StackFrame("app.py", line, "handler"),)

    first = stacks._intern(frames(1))
    second = stacks._intern(frames(2))
    third = stacks._intern(frames(3))
    fourth = stacks._intern(frames(4))

    assert first != second
    assert third == fourth
    assert stack_frames(third) == stacks._OVERFLOW

    # An already interned stack still resolves to itself at the bound.

    assert stacks._intern(frames(1)) == first


def test_clear_stacks_releases_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wrapture import clear_stacks, stacks

    monkeypatch.setattr(stacks, "_ids", {})
    monkeypatch.setattr(stacks, "_stacks", [])

    frames = (stacks.StackFrame("app.py", 1, "handler"),)
    stale = stacks._intern(frames)

    clear_stacks()

    # Old ids stop resolving, loudly; new captures start afresh.

    with pytest.raises(KeyError, match="may have been cleared"):
        stack_frames(stale)

    assert stacks._intern(frames) == 0
