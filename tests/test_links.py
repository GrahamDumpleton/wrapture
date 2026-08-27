"""Tests for event links: the causal-but-not-nested relationship a
detached root carries back to the operation that handed it off.

detach() is the thread hand-off, handoff() the queue producer's
capture, block(links=) the consumer's declaration; the tape resolves
the relationship with origins_of() and detached_from(), and every
renderer and the OpenTelemetry sink translate it. The one rule
under test throughout: contained means child, never linked.
"""

import io
import json
import threading
import time
import warnings
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import wrapture
from wrapture import (
    ConfigWarning,
    EventLink,
    Printer,
    binding,
    block,
    canonical,
    chrome_trace,
    detach,
    handoff,
    observed,
    propagate,
    timeline,
)
from wrapture.sinks import _event_record, add_sink, remove_sink
from wrapture.trace import _configure, _restore

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@pytest.fixture
def trace_disabled() -> Iterator[None]:
    previous = _configure(False)
    try:
        yield
    finally:
        _restore(previous)


class Ledger:
    def record(self, entry: str) -> str:
        return f"recorded:{entry}"


class Uploads:
    def handle(self, name: str) -> str:
        self.store(name)
        self.thread = threading.Thread(target=detach(self.thumbnails), args=(name,))
        self.thread.start()
        return "accepted"

    def store(self, name: str) -> None:
        pass

    def thumbnails(self, name: str) -> None:
        for size in (32, 64):
            self.resize(name, size)

    def resize(self, name: str, size: int) -> str:
        return f"{name}@{size}"


def _slot(event: Any) -> Any:
    assert event.trace is not None
    return event.trace.slots["w3c"]


def _run(fn: Any, *args: Any) -> None:
    thread = threading.Thread(target=fn, args=args)
    thread.start()
    thread.join()


# ---------------------------------------------------------------------------
# detach(): the thread hand-off
# ---------------------------------------------------------------------------


def test_a_detached_thread_records_a_linked_root_of_its_own() -> None:
    handle = binding(Uploads, "handle")
    thumbnails = binding(Uploads, "thumbnails")
    resize = binding(Uploads, "resize")

    with timeline(handle, thumbnails, resize) as tape:
        uploads = Uploads()
        uploads.handle("cat.png")
        uploads.thread.join()

    upload, thumbs = tape.roots()
    assert upload.binding is handle
    assert thumbs.binding is thumbnails

    # Two trees, not one: the thread's work is a root with no parent,
    # carrying a link to the event in flight at the hand-off, and its
    # own children nest under it as normal.

    assert thumbs.parent_id is None
    assert thumbs.depth == 0
    assert thumbs.links == (
        EventLink(
            trace_id=_slot(upload).trace_id,
            span_id=_slot(upload).span_id,
            seq=upload.seq,
        ),
    )
    assert [child.binding for child in tape.children_of(thumbs)] == [resize, resize]
    assert all(child.links == () for child in tape.children_of(thumbs))

    # Each tree is its own trace: the detached root minted.

    assert thumbs.trace is not None and upload.trace is not None
    assert thumbs.trace is not upload.trace
    assert _slot(thumbs).trace_id != _slot(upload).trace_id


def test_the_callers_duration_excludes_the_detached_work() -> None:
    release = threading.Event()

    @observed
    def slow() -> None:
        release.wait(timeout=5)

    @observed
    def request() -> None:
        thread = threading.Thread(target=detach(slow))
        thread.start()
        threads.append(thread)

    threads: list[threading.Thread] = []

    with timeline() as tape:
        request()
        time.sleep(0.02)
        release.set()
        threads[0].join()

    req, work = tape.roots()
    assert req.duration is not None and work.duration is not None
    assert work.duration > req.duration
    assert tape.origins_of(work) == [req]
    assert tape.detached_from(req) == [work]


def test_every_root_on_the_detached_thread_links_back() -> None:
    # The origin persists for the life of the detached context: an
    # unobserved target calling two observed functions in sequence
    # yields two linked roots, not one linked and one stray.

    record = binding(Ledger, "record")

    def work() -> None:
        Ledger().record("one")
        Ledger().record("two")

    with timeline(record) as tape:
        with block("origin"):
            _run(detach(work))

    origin, one, two = tape.all
    assert one.parent_id is None and two.parent_id is None
    assert [link.seq for link in one.links] == [origin.seq]
    assert [link.seq for link in two.links] == [origin.seq]
    assert tape.detached_from(origin) == [one, two]


def test_detach_records_nothing_itself() -> None:
    record = binding(Ledger, "record")

    def quiet() -> str:
        return "no observed calls here"

    with timeline(record) as tape:
        with block("origin"):
            _run(detach(quiet))

    assert [event.label for event in tape.all] == ["origin"]


def test_detach_outside_any_operation_yields_unlinked_roots() -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape:
        _run(detach(Ledger().record), "bare")

    (event,) = tape.all
    assert event.links == ()
    assert event.parent_id is None


def test_a_detached_pool_task_links_back() -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape, ThreadPoolExecutor(max_workers=2) as pool:
        with block("request"):
            future = pool.submit(detach(Ledger().record), "pooled")
        future.result()

    request, pooled = tape.all
    assert pooled.parent_id is None
    assert tape.origins_of(pooled) == [request]


def test_one_detached_callable_is_shared_by_several_threads() -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape:
        with block("fanout"):
            work = detach(Ledger().record)
            threads = [threading.Thread(target=work, args=(f"t{n}",)) for n in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

    fanout = tape.all[0]
    assert len(tape.detached_from(fanout)) == 4
    assert all(event.parent_id is None for event in tape.detached_from(fanout))


def test_an_origin_captured_earlier_can_be_handed_to_detach() -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape:
        with block("first"):
            origin = handoff()
        with block("second"):
            _run(detach(Ledger().record, origin=origin), "late")

    first, second, late = tape.all
    assert [link.seq for link in late.links] == [first.seq]
    assert tape.origins_of(late) == [first]
    assert tape.detached_from(second) == []


def test_propagate_still_nests_where_detach_links() -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape:
        with block("origin"):
            _run(propagate(Ledger().record), "nested")
            _run(detach(Ledger().record), "detached")

    origin, nested, detached = tape.all
    assert nested.parent_id == origin.seq and nested.links == ()
    assert detached.parent_id is None and detached.links[0].seq == origin.seq


def test_a_detached_thread_outliving_the_timeline_discards_visibly() -> None:
    record = binding(Ledger, "record")
    release = threading.Event()

    def late() -> None:
        release.wait(timeout=5)
        Ledger().record("late")

    with record:
        with timeline() as tape:
            with block("origin"):
                thread = threading.Thread(target=detach(late))
                thread.start()

        release.set()
        thread.join()

    assert [event.label for event in tape.all] == ["origin"]
    assert tape.discarded == 1


def test_a_nested_call_in_the_detached_thread_is_a_child_not_a_link() -> None:
    record = binding(Ledger, "record")

    def work() -> None:
        with block("inner"):
            Ledger().record("deep")

    with timeline(record) as tape:
        with block("origin"):
            _run(detach(work))

    origin, inner, deep = tape.all
    assert inner.links[0].seq == origin.seq
    assert deep.parent_id == inner.seq
    assert deep.links == ()


# ---------------------------------------------------------------------------
# handoff(): capturing the origin
# ---------------------------------------------------------------------------


def test_handoff_outside_any_operation_is_none() -> None:
    with timeline():
        assert handoff() is None


def test_handoff_snapshots_the_events_identity_and_headers() -> None:
    with timeline() as tape:
        with block("producer"):
            origin = handoff()
            headers = wrapture.trace_headers()

    assert origin is not None
    (event,) = tape.all
    slot = _slot(event)

    assert origin.link == EventLink(
        trace_id=slot.trace_id, span_id=slot.span_id, seq=event.seq
    )
    assert origin.headers() == headers
    assert origin.headers()["traceparent"].split("-")[1:3] == [
        slot.trace_id,
        slot.span_id,
    ]
    assert (
        repr(origin) == f"<Handoff from #{event.seq}: {slot.trace_id}/{slot.span_id}>"
    )


def test_handoff_without_a_trace_identity_still_links_by_seq(
    trace_disabled: None,
) -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape:
        with block("origin"):
            origin = handoff()
            _run(detach(Ledger().record), "x")

    assert origin is not None
    assert origin.link == EventLink(seq=tape.all[0].seq)
    assert origin.headers() == {}

    detached = tape.all[1]
    assert detached.links == (EventLink(seq=tape.all[0].seq),)
    assert tape.origins_of(detached) == [tape.all[0]]


# ---------------------------------------------------------------------------
# block(links=): the consumer side
# ---------------------------------------------------------------------------


def test_a_root_block_links_to_the_message_headers() -> None:
    with timeline() as tape:
        with block("consume", links=[{"Traceparent": TRACEPARENT, "x": "y"}]):
            pass

    (event,) = tape.all
    assert event.links == (
        EventLink(
            trace_id="0af7651916cd43dd8448eb211c80319c", span_id="b7ad6b7169203331"
        ),
    )
    assert event.links[0].seq is None
    assert tape.origins_of(event) == []


def test_a_handoff_round_trips_through_a_message() -> None:
    queue: list[dict[str, Any]] = []

    with timeline() as tape:
        with block("produce"):
            origin = handoff()
            assert origin is not None
            queue.append({"headers": origin.headers()})

        message = queue.pop()
        with block("consume", links=[message["headers"]]):
            pass

    produce, consume = tape.all
    slot = _slot(produce)
    assert consume.links == (EventLink(trace_id=slot.trace_id, span_id=slot.span_id),)


def test_event_links_pass_through_and_headers_without_a_traceparent_are_skipped() -> (
    None
):
    link = EventLink(trace_id="a" * 32, span_id="b" * 16, attributes={"queue": "jobs"})

    with timeline() as tape:
        with block("consume", links=[link, {}, {"traceparent": "garbage"}]):
            pass

    assert tape.all[0].links == (link,)


def test_links_of_the_wrong_type_are_refused() -> None:
    with pytest.raises(TypeError, match="EventLinks or header mappings"):
        block("consume", links=[42])  # type: ignore[list-item]


def test_links_on_a_nested_block_are_dropped_with_a_warning() -> None:
    with timeline() as tape:
        with block("outer"):
            with warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter("always")
                with block("inner", links=[{"traceparent": TRACEPARENT}]):
                    pass

    outer, inner = tape.all
    assert inner.parent_id == outer.seq
    assert inner.links == ()

    (warning,) = seen
    assert issubclass(warning.category, ConfigWarning)
    assert "links= ignored" in str(warning.message)


def test_explicit_links_win_over_the_detached_origin() -> None:
    def consume() -> None:
        with block("consume", links=[{"traceparent": TRACEPARENT}]):
            pass

    with timeline() as tape:
        with block("origin"):
            _run(detach(consume))

    consume_event = tape.all[1]
    assert consume_event.links[0].trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert consume_event.links[0].seq is None


# ---------------------------------------------------------------------------
# rendering: tree(), the Printer, JSONLines and the exporters
# ---------------------------------------------------------------------------


def _traced() -> Any:
    handle = binding(Uploads, "handle", label="handle")
    thumbnails = binding(Uploads, "thumbnails", label="thumbnails")
    resize = binding(Uploads, "resize", label="resize")

    with timeline(handle, thumbnails, resize) as tape:
        uploads = Uploads()
        uploads.handle("cat.png")
        uploads.thread.join()

    return tape


def test_tree_names_the_origin_of_a_detached_root() -> None:
    tape = _traced()
    lines = tape.tree().splitlines()

    assert lines[0].startswith("handle(")
    assert lines[1] == "thumbnails(name='cat.png')  -> None  <- handle"
    assert lines[2].startswith("  resize(")


def test_tree_falls_back_to_the_trace_id_for_a_remote_origin() -> None:
    with timeline() as tape:
        with block("consume", links=[{"traceparent": TRACEPARENT}]):
            pass

    assert tape.tree() == "block: consume  <- trace 0af7651916cd43dd8448eb211c80319c"


def test_tree_falls_back_to_the_seq_when_nothing_else_identifies_the_origin(
    trace_disabled: None,
) -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape:
        with block("origin"):
            _run(detach(Ledger().record), "x")

    detached = tape.all[1]
    assert tape.within(detached).tree() == ""
    assert f"<- #{tape.all[0].seq}" not in tape.tree()  # resolvable on the tape
    assert "<- origin" in tape.tree()

    # Rendered from the printer, which holds no events to resolve
    # against and has no trace id to fall back to, the seq is all
    # there is.

    output = io.StringIO()
    printer = add_sink(Printer(output, timing=False))
    try:
        with timeline(record) as tape:
            with block("origin"):
                _run(detach(Ledger().record), "x")
    finally:
        remove_sink(printer)

    assert f"  <- #{tape.all[0].seq}" in output.getvalue()


def test_the_printer_marks_a_detached_root_with_its_origins_trace_id() -> None:
    record = binding(Ledger, "record")
    output = io.StringIO()

    printer = add_sink(Printer(output, timing=False))
    try:
        with timeline(record) as tape:
            with block("origin"):
                _run(detach(Ledger().record), "x")
    finally:
        remove_sink(printer)

    trace_id = _slot(tape.all[0]).trace_id
    lines = output.getvalue().splitlines()
    assert lines[1] == f"test_links:Ledger.record(entry='x')  <- trace {trace_id}"


def test_the_record_carries_the_links() -> None:
    tape = _traced()
    upload, thumbs = tape.roots()
    slot = _slot(upload)

    record = _event_record(thumbs)
    assert record["links"] == [
        {"trace_id": slot.trace_id, "span_id": slot.span_id, "seq": upload.seq}
    ]
    assert "links" not in _event_record(upload)

    link = EventLink(seq=3, attributes={"queue": "jobs", "message_id": 7})
    event = wrapture.Event("block", "x", links=(link,))
    assert _event_record(event)["links"] == [
        {"seq": 3, "attributes": {"queue": "jobs", "message_id": 7}}
    ]


def test_canonical_marks_a_detached_root_with_its_origins_path() -> None:
    tape = _traced()

    assert canonical(tape) == (
        "call test_links:Uploads.handle\n"
        "call test_links:Uploads.thumbnails <- test_links:Uploads.handle\n"
        "  call test_links:Uploads.resize\n"
        "  call test_links:Uploads.resize"
    )


def test_canonical_leaves_a_remote_origin_out() -> None:
    with timeline() as tape:
        with block("consume", links=[{"traceparent": TRACEPARENT}]):
            pass

    assert (
        canonical(tape) == "block test_links:test_canonical_leaves_a_remote_origin_out"
    )


def test_chrome_trace_draws_a_flow_from_origin_to_detached_root() -> None:
    tape = _traced()
    upload, thumbs = tape.roots()

    trace = json.loads(chrome_trace(tape))
    flows = [entry for entry in trace["traceEvents"] if entry["ph"] in ("s", "f")]
    slices = {
        entry["args"]["seq"]: entry
        for entry in trace["traceEvents"]
        if entry["ph"] == "X"
    }

    start, finish = flows
    assert start["ph"] == "s" and finish["ph"] == "f"
    assert start["id"] == finish["id"]
    assert start["bp"] == "e" and finish["bp"] == "e"
    assert start["tid"] == slices[upload.seq]["tid"]
    assert finish["tid"] == slices[thumbs.seq]["tid"]
    assert start["ts"] == slices[upload.seq]["ts"]
    assert finish["ts"] == slices[thumbs.seq]["ts"]
    assert start["tid"] != finish["tid"]

    assert slices[thumbs.seq]["args"]["links"][0]["seq"] == upload.seq


def test_chrome_trace_skips_flows_for_remote_origins() -> None:
    with timeline() as tape:
        with block("consume", links=[{"traceparent": TRACEPARENT}]):
            pass

    trace = json.loads(chrome_trace(tape))
    assert not [entry for entry in trace["traceEvents"] if entry["ph"] in ("s", "f")]
    (slice_entry,) = [entry for entry in trace["traceEvents"] if entry["ph"] == "X"]
    assert slice_entry["args"]["links"][0]["trace_id"] == (
        "0af7651916cd43dd8448eb211c80319c"
    )
