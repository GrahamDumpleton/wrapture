"""Tests for declaring a stretch of code as a block event.

These cover the "block" event kind end to end: what one use of the
context manager becomes, how keyword arguments and annotate() fill
its data, nesting in the event tree, the recording gates (inert with
no sinks, the reentrancy guard), trace identity at a block root,
thread isolation, selection with tape.blocks(), ordering assertions,
rendering by the printer and the tape's tree, and serialisation.
"""

import contextvars
import io
import sys
import threading
from typing import Any

import pytest
from wrapt import MISSING

import wrapture
from wrapture import EventLog, observed, timeline
from wrapture.events import Event
from wrapture.sinks import Printer, Sink, _event_record
from wrapture.trace import _configure, _restore


class Collector(Sink):
    """A process sink recording enters and exits, for the tests that
    must not use a timeline."""

    capture_args = "none"
    capture_result = "none"

    def __init__(self) -> None:
        self.entered: list[Event] = []
        self.exited: list[Event] = []

    def on_enter(self, event: Event) -> None:
        self.entered.append(event)

    def on_exit(self, event: Event) -> None:
        self.exited.append(event)


# ---------------------------------------------------------------------------
# what one block becomes
# ---------------------------------------------------------------------------


def test_a_block_becomes_one_event_with_its_details() -> None:
    with timeline() as tape:
        with wrapture.block("render-invoice", customer=42):
            pass

    event = tape.all[0]

    assert event.kind == "block"
    assert event.label == "render-invoice"
    assert event.data["customer"] == 42
    assert event.finished
    assert event.duration is not None and event.duration >= 0.0
    assert event.result is MISSING
    assert event.args is None and event.kwargs is None


def test_the_path_locates_the_enclosing_function() -> None:
    def process() -> None:
        with wrapture.block("inside"):
            pass

    with timeline() as tape:
        process()

    event = tape.all[0]
    assert event.path == (
        "test_blocks:test_the_path_locates_the_enclosing_function.<locals>.process"
    )


def test_annotate_merges_into_the_block() -> None:
    with timeline() as tape:
        with wrapture.block("render", customer=1):
            wrapture.annotate(pages=4)

    assert tape.all[0].data == {"customer": 1, "pages": 4}


def test_current_event_names_the_block_inside() -> None:
    with timeline():
        with wrapture.block("phase"):
            event = wrapture.current_event()
            assert event is not None and event.label == "phase"


def test_the_event_renders_with_the_kind_prefix() -> None:
    with timeline() as tape:
        with wrapture.block("first request"):
            pass

    assert str(tape.all[0]) == "block: first request"


def test_a_name_must_be_a_non_empty_string() -> None:
    with pytest.raises(TypeError, match="non-empty name"):
        wrapture.block("")

    with pytest.raises(TypeError, match="non-empty name"):
        wrapture.block(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# nesting
# ---------------------------------------------------------------------------


@observed
def leaf() -> str:
    return "done"


def test_events_inside_the_body_nest_under_the_block() -> None:
    with timeline() as tape:
        with wrapture.block("outer"):
            leaf()

    outer, call = tape.all
    assert call.parent_id == outer.seq
    assert call.depth == 1
    assert tape.parent_of(call) is outer


def test_blocks_nest_inside_each_other_and_inside_calls() -> None:
    @observed
    def process() -> None:
        with wrapture.block("phase"):
            with wrapture.block("step"):
                pass

    with timeline() as tape:
        process()

    call, phase, step = tape.all
    assert phase.parent_id == call.seq
    assert step.parent_id == phase.seq
    assert [event.depth for event in tape.all] == [0, 1, 2]


# ---------------------------------------------------------------------------
# outcome
# ---------------------------------------------------------------------------


def test_an_escaping_exception_is_recorded_and_propagates() -> None:
    with timeline() as tape:
        with pytest.raises(ValueError, match="boom"):
            with wrapture.block("failing"):
                raise ValueError("boom")

    event = tape.all[0]
    assert isinstance(event.exception, ValueError)
    assert event.finished


# ---------------------------------------------------------------------------
# the recording gates
# ---------------------------------------------------------------------------


def test_inert_when_nothing_listens() -> None:
    with wrapture.block("nobody-listening", cost=1):
        assert not wrapture.current_event()


def test_a_sink_that_uses_a_block_does_not_recurse() -> None:
    class BlockingSink(Collector):
        def on_enter(self, event: Event) -> None:
            super().on_enter(event)
            with wrapture.block("from the sink"):
                pass

    sink = BlockingSink()

    wrapture.add_sink(sink)
    try:
        with wrapture.block("application speaking"):
            pass
    finally:
        wrapture.remove_sink(sink)

    assert [event.label for event in sink.entered] == ["application speaking"]


def test_reusing_an_active_block_raises() -> None:
    with timeline():
        active = wrapture.block("once")

        with active:
            with pytest.raises(RuntimeError, match="already active"):
                with active:
                    pass


def test_sequential_reuse_records_two_events() -> None:
    marker = wrapture.block("pass")

    with timeline() as tape:
        with marker:
            pass
        with marker:
            pass

    assert len(tape.blocks("pass")) == 2


# ---------------------------------------------------------------------------
# trace identity
# ---------------------------------------------------------------------------


def test_a_root_block_mints_a_trace() -> None:
    with timeline() as tape:
        with wrapture.block("process-batch"):
            headers = wrapture.trace_headers()

    event = tape.all[0]
    assert event.trace is not None
    assert headers["traceparent"].split("-")[1] == (event.trace.slots["w3c"].trace_id)


def test_a_nested_block_shares_the_tree_trace() -> None:
    with timeline() as tape:
        with wrapture.block("outer"):
            with wrapture.block("inner"):
                pass

    outer, inner = tape.all
    assert outer.trace is not None
    assert inner.trace is outer.trace


def test_no_identity_when_disabled() -> None:
    previous = _configure(False)
    try:
        with timeline() as tape:
            with wrapture.block("untraced"):
                pass
    finally:
        _restore(previous)

    assert tape.all[0].trace is None


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------


def test_a_block_in_one_thread_is_invisible_to_another() -> None:
    # The ambient stack is a contextvar, so a block opened here is not
    # the parent of work recorded on a thread that does not carry this
    # context; the process sink hears both, unrelated. On 3.14+ the
    # thread gets a fresh empty context explicitly, since some builds
    # inherit a copy of the caller's by default.

    sink = Collector()

    extra: dict[str, Any] = {}
    if sys.version_info >= (3, 14):
        extra["context"] = contextvars.Context()

    wrapture.add_sink(sink)
    try:
        with wrapture.block("main thread"):
            worker = threading.Thread(target=leaf, **extra)
            worker.start()
            worker.join()
    finally:
        wrapture.remove_sink(sink)

    block_event, call = sink.entered
    assert call.parent_id is None
    assert call.depth == 0
    assert block_event.label == "main thread"


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def test_blocks_selects_by_pattern() -> None:
    with timeline() as tape:
        with wrapture.block("render header"):
            pass
        with wrapture.block("render body"):
            leaf()
        with wrapture.block("flush"):
            pass

    assert len(tape.blocks()) == 3
    assert [event.label for event in tape.blocks("render *")] == [
        "render header",
        "render body",
    ]
    assert tape.blocks("flush").assert_once().first.label == "flush"

    # Only block events are selected, and the pattern is
    # case-sensitive.

    assert len(tape.blocks("Render *")) == 0
    assert all(event.kind == "block" for event in tape.blocks())


def test_blocks_composes_with_the_assertion_family() -> None:
    with timeline() as tape:
        with pytest.raises(ValueError):
            with wrapture.block("risky"):
                raise ValueError("no")
        with wrapture.block("safe"):
            pass

    tape.blocks("risky").raising(ValueError).assert_once()
    tape.blocks("safe").raising(ValueError).assert_never()


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_blocks_participate_in_assert_order() -> None:
    with timeline() as tape:
        with wrapture.block("setup"):
            pass
        leaf()
        with wrapture.block("teardown"):
            pass

    tape.assert_order(tape.blocks("setup"), leaf, tape.blocks("teardown"))

    with pytest.raises(AssertionError, match="stalled"):
        tape.assert_order(tape.blocks("teardown"), tape.blocks("setup"))


def test_strictness_counts_same_named_blocks_between_steps() -> None:
    # Blocks of one name share a recorder identity, so the strictness
    # flags treat them the way they treat one binding's calls: an
    # unmatched "phase" block between two matched ones breaks a
    # consecutive run.

    with timeline() as tape:
        with wrapture.block("phase", n=1):
            pass
        with wrapture.block("phase", n=2):
            pass
        with wrapture.block("phase", n=3):
            pass

    first, second, third = tape.blocks("phase")

    with pytest.raises(AssertionError, match="consecutive"):
        tape.assert_order(
            EventLog("first phase", [first]),
            EventLog("third phase", [third]),
            consecutive=True,
        )


def test_strictness_ignores_blocks_the_steps_never_name() -> None:
    with timeline() as tape:
        with wrapture.block("setup"):
            pass
        with wrapture.block("unrelated"):
            pass
        with wrapture.block("teardown"):
            pass

    tape.assert_order(tape.blocks("setup"), tape.blocks("teardown"), consecutive=True)


# ---------------------------------------------------------------------------
# subtree views
# ---------------------------------------------------------------------------


@observed
def caller() -> str:
    result: str = leaf()
    return result


def test_within_scopes_the_query_face() -> None:
    with timeline() as tape:
        with wrapture.block("first request"):
            leaf()
        with wrapture.block("second request"):
            pass

    second = tape.blocks("second request").assert_once().first

    tape.for_binding(leaf).assert_once()
    tape.within(second).for_binding(leaf).assert_never()

    first = tape.blocks("first request").first
    tape.within(first).for_binding(leaf).assert_once()


def test_within_sees_deep_descendants_not_just_direct_children() -> None:
    # The membership is the whole subtree: a call nested two levels
    # down is inside the block even though its direct parent is not
    # the block itself.

    with timeline() as tape:
        with wrapture.block("work"):
            caller()

    view = tape.within(tape.blocks("work").first)

    view.for_binding(leaf).assert_once()
    assert [event.path for event in view.all] == [
        "test_blocks:caller",
        "test_blocks:leaf",
    ]


def test_within_boundary_behaviour() -> None:
    with timeline() as tape:
        with wrapture.block("outer"):
            with wrapture.block("inner"):
                leaf()

    outer = tape.blocks("outer").first
    inner = tape.blocks("inner").first
    view = tape.within(outer)

    # The container is not a member of its own view; it is the view's
    # root, the view's roots are its direct children, and parent_of on
    # a direct child returns the real container event.

    assert view.root is outer
    assert outer not in view.all
    assert view.roots() == [inner]
    assert view.parent_of(inner) is outer

    call = view.for_binding(leaf).first
    assert view.parent_of(call) is inner


def test_within_scopes_assert_order() -> None:
    with timeline() as tape:
        leaf()
        with wrapture.block("work"):
            leaf()

    view = tape.within(tape.blocks("work").first)

    # The call outside the block is invisible to the view, so the one
    # inside is exactly what the view recorded; on the whole tape the
    # same expectation breaks on the outside call.

    view.assert_order(leaf, exact=True)

    with pytest.raises(AssertionError, match="exactly"):
        tape.assert_order(leaf, exact=True)


def test_within_tree_draws_from_the_margin() -> None:
    with timeline() as tape:
        with wrapture.block("work"):
            with wrapture.block("step"):
                leaf()

    assert tape.within(tape.blocks("work").first).tree() == (
        "block: step\n  test_blocks:leaf()  -> 'done'"
    )


def test_views_nest() -> None:
    with timeline() as tape:
        with wrapture.block("outer"):
            with wrapture.block("inner"):
                leaf()
            leaf()

    outer_view = tape.within(tape.blocks("outer").first)
    inner_view = outer_view.within(tape.blocks("inner").first)

    assert len(outer_view.for_binding(leaf)) == 2
    inner_view.for_binding(leaf).assert_once()


def test_the_view_is_live() -> None:
    with timeline() as tape:
        with wrapture.block("work"):
            pass

        view = tape.within(tape.blocks("work").first)
        assert view.all == []

        # More events arriving under the block after the view was
        # created are visible through it, as they are through the
        # tape. A block cannot be re-entered, so drive the tape
        # directly with a nested event.

        late = Event("call", "svc:late", label="late")
        late.parent_id = view.root.seq
        late.seq = view.root.seq + 1
        tape.on_enter(late)

        assert view.all == [late]


# ---------------------------------------------------------------------------
# rendering, serialisation
# ---------------------------------------------------------------------------


def test_the_tree_gains_narrative_structure() -> None:
    with timeline() as tape:
        with wrapture.block("first request"):
            leaf()
        with wrapture.block("second request"):
            pass

    assert tape.tree() == (
        "block: first request\n  test_blocks:leaf()  -> 'done'\nblock: second request"
    )


def test_the_printer_prints_the_block_lines() -> None:
    stream = io.StringIO()
    printer = Printer(stream, timing=False)

    wrapture.add_sink(printer)
    try:
        with wrapture.block("render-invoice"):
            pass
        with pytest.raises(ValueError):
            with wrapture.block("failing"):
                raise ValueError("no")
    finally:
        wrapture.remove_sink(printer)

    # A successful block with timing off has nothing to close with, so
    # only its opening line appears; a failing one closes with the
    # exception marker.

    assert stream.getvalue() == (
        "block: render-invoice\nblock: failing\nfailing !! ValueError\n"
    )


def test_the_serialised_record_carries_the_block_fields() -> None:
    with timeline() as tape:
        with wrapture.block("flush", rows=10):
            pass

    record = _event_record(tape.all[0])

    assert record["kind"] == "block"
    assert record["label"] == "flush"
    assert record["path"].startswith("test_blocks:")
    assert record["data"] == {"rows": 10}
    assert record["duration"] == tape.all[0].duration
    assert "trace" in record
