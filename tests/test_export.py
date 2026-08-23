"""Tests for the exporters: Chrome trace JSON, the canonical snapshot
form, and Mermaid sequence diagrams.

Every exporter accepts either a Tape or serialised event records, so
the same trace can be exported live from a test or from a JSONLines
file long after the run. The serialised form carries thread identity,
which Chrome trace turns into per-thread lanes.
"""

import json
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from wrapture import (
    binding,
    canonical,
    chrome_trace,
    load_events,
    mermaid,
    note_exception,
    timeline,
)
from wrapture.sinks import _event_record


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"

    def refund(self, amount: int) -> str:
        raise TimeoutError("gateway offline")


class Processor:
    def process(self) -> str:
        return Gateway().charge(500)


class Feed:
    def stream(self, count: int) -> Generator[int, None, None]:
        yield from range(count)


class Model:
    status = "draft"


class Handler:
    def dispatch(self) -> str:
        # The framework shape: the failure is caught and noted against
        # this scope, which then completes normally.

        try:
            return Gateway().refund(2)
        except TimeoutError as exc:
            note_exception(exc)
            return "handled"


@pytest.fixture
def traced() -> Any:
    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")
    status = binding(Model, "status")
    dispatch = binding(Handler, "dispatch")

    with timeline(process, charge, refund, status, dispatch) as tape:
        Processor().process()

        with pytest.raises(TimeoutError):
            Gateway().refund(1)

        Model().status = "published"

        Handler().dispatch()

    return tape


# ---------------------------------------------------------------------------
# chrome trace
# ---------------------------------------------------------------------------


def test_chrome_trace_nests_slices_on_a_thread_lane(traced: Any) -> None:
    trace = json.loads(chrome_trace(traced))
    slices = {
        entry["name"]: entry for entry in trace["traceEvents"] if entry["ph"] == "X"
    }

    parent = slices["Processor.process"]
    child = slices["Gateway.charge"]

    # The child slice sits inside the parent's interval, on the same
    # thread lane, and timestamps are normalised to start at zero.

    assert child["ts"] >= parent["ts"]
    assert child["ts"] + child["dur"] <= parent["ts"] + parent["dur"] + 1
    assert child["tid"] == parent["tid"] == threading.get_ident()
    assert min(entry["ts"] for entry in slices.values()) == 0

    assert parent["args"]["result"] == "ch_500"

    # An escaped exception and a noted one both reach the detail pane.

    refunds = [
        entry
        for entry in trace["traceEvents"]
        if entry["ph"] == "X" and entry["name"] == "Gateway.refund"
    ]
    assert all(e["args"]["exception"]["type"] == "TimeoutError" for e in refunds)
    assert len(refunds) == 2

    dispatch = slices["Handler.dispatch"]
    assert dispatch["args"]["result"] == "handled"
    assert "exception" not in dispatch["args"]
    (caught,) = dispatch["args"]["caught"]
    assert caught["type"] == "TimeoutError"
    assert caught["message"] == "gateway offline"
    assert 0.0 <= caught["offset"] <= dispatch["dur"] / 1e6


def test_chrome_trace_names_the_thread_lanes(traced: Any) -> None:
    trace = json.loads(chrome_trace(traced))
    metadata = [entry for entry in trace["traceEvents"] if entry["ph"] == "M"]

    names = {entry["name"]: entry["args"]["name"] for entry in metadata}
    assert names["process_name"] == "wrapture"
    assert names["thread_name"] == threading.current_thread().name


def test_records_from_other_threads_get_their_own_lanes() -> None:
    # Hand-built records double as the proof that exporters accept
    # plain dicts in the serialised form, not just tapes.

    records = [
        {
            "seq": 1,
            "parent_id": None,
            "depth": 0,
            "kind": "call",
            "path": "app:main",
            "started": 0.0,
            "duration": 0.01,
            "thread_id": 111,
            "thread_name": "MainThread",
        },
        {
            "seq": 2,
            "parent_id": None,
            "depth": 0,
            "kind": "call",
            "path": "app:work",
            "started": 0.002,
            "duration": 0.005,
            "thread_id": 222,
            "thread_name": "worker-1",
        },
    ]

    trace = json.loads(chrome_trace(records))

    lanes = {
        entry["tid"]: entry["args"]["name"]
        for entry in trace["traceEvents"]
        if entry["ph"] == "M" and entry["name"] == "thread_name"
    }
    assert lanes == {111: "MainThread", 222: "worker-1"}

    tids = {
        entry["name"]: entry["tid"]
        for entry in trace["traceEvents"]
        if entry["ph"] == "X"
    }
    assert tids == {"app:main": 111, "app:work": 222}


def test_an_event_that_never_closed_becomes_a_begin_only_slice() -> None:
    stream = binding(Feed, "stream")

    with timeline(stream) as tape:
        # Created but never iterated: the event has begun and has no
        # duration yet, which the trace must show rather than invent.

        suspended = Feed().stream(3)
        trace = json.loads(chrome_trace(tape))

    del suspended

    (entry,) = [e for e in trace["traceEvents"] if e["ph"] not in ("M",)]
    assert entry["ph"] == "B"
    assert "dur" not in entry


def test_a_tape_and_its_serialised_records_export_identically(
    traced: Any, tmp_path: Path
) -> None:
    # Write the records in completion order, the order a JSONLines
    # file would hold, and export both ways.

    records = [_event_record(event) for event in traced.all]
    source = tmp_path / "trace.jsonl"
    source.write_text(
        "".join(json.dumps(record) + "\n" for record in reversed(records))
    )

    assert json.loads(chrome_trace(load_events(source))) == json.loads(
        chrome_trace(traced)
    )
    assert canonical(load_events(source)) == canonical(traced)


# ---------------------------------------------------------------------------
# the canonical form
# ---------------------------------------------------------------------------


def test_canonical_is_a_stable_architectural_fingerprint(traced: Any) -> None:
    assert canonical(traced) == (
        f"call {__name__}:Processor.process\n"
        f"  call {__name__}:Gateway.charge\n"
        f"call {__name__}:Gateway.refund !! TimeoutError\n"
        f"set {__name__}:Model.status\n"
        f"call {__name__}:Handler.dispatch !! TimeoutError\n"
        f"  call {__name__}:Gateway.refund !! TimeoutError"
    )


def test_canonical_marks_injected_outcomes() -> None:
    charge = binding(Gateway, "charge").on_call.returns("stubbed")

    with timeline(charge) as tape:
        Gateway().charge(1)

    assert canonical(tape) == f"call {__name__}:Gateway.charge (injected)"


# ---------------------------------------------------------------------------
# mermaid
# ---------------------------------------------------------------------------


def test_mermaid_renders_the_trace_as_a_sequence_diagram(traced: Any) -> None:
    lines = mermaid(traced).splitlines()

    assert lines[0] == "sequenceDiagram"
    assert "    participant caller" in lines

    # Containers become aliased participants under short display
    # names, since Mermaid identifiers cannot carry colons or dots.

    assert "    participant P1 as Processor" in lines
    assert "    participant P2 as Gateway" in lines
    assert "    participant P3 as Model" in lines

    # Nesting becomes activation: calls go out with ->>+ and every
    # event returns, a failure returning its exception type and an
    # attribute event carrying its kind.

    assert lines.index("    caller->>+P1: process") < lines.index(
        "    P1->>+P2: charge"
    )
    assert "    P2-->>-P1: return" in lines
    assert "    P1-->>-caller: return" in lines
    assert "    P2-->>-caller: TimeoutError" in lines
    assert "    caller->>+P3: status (set)" in lines

    # A noted exception follows the outcome: the scope returned, and
    # it failed.

    assert "    participant P4 as Handler" in lines
    assert "    P2-->>-P4: TimeoutError" in lines
    assert "    P4-->>-caller: return !! TimeoutError" in lines


def test_mermaid_disambiguates_clashing_short_names() -> None:
    records: list[dict[str, Any]] = [
        {
            "seq": 1,
            "parent_id": None,
            "kind": "call",
            "path": "billing.api:Gateway.charge",
        },
        {"seq": 2, "parent_id": 1, "kind": "call", "path": "shipping.api:Gateway.book"},
    ]

    lines = mermaid(records).splitlines()

    assert "    participant P1 as billing.api:Gateway" in lines
    assert "    participant P2 as shipping.api:Gateway" in lines
