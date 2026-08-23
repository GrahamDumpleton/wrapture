"""Exporters: recorded events rendered in other tools' formats.

Every exporter walks the same input: either a Tape, or an iterable of
event records in the serialised form JSONLines writes, so a trace can
be exported live from a test or long after the fact from a file the
runner produced. load_events() reads such a file back.

Three renderings, each aimed at an existing consumer rather than a
viewer of our own: Chrome trace JSON for the Perfetto timeline UI,
a canonical text tree for snapshot tests, and a Mermaid sequence
diagram for anywhere Mermaid renders.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from typing import Any

from .sinks import _event_record
from .timeline import Tape

TraceSource = Tape | Iterable[Mapping[str, Any]]


def _records(source: TraceSource) -> list[dict[str, Any]]:
    # Normalise either input to records in recording order. A tape
    # snapshot is already seq-sorted; serialised input arrives in
    # completion order, so it is sorted here.

    if isinstance(source, Tape):
        return [_event_record(event) for event in source.all]

    records = [dict(record) for record in source]
    records.sort(key=lambda record: record.get("seq", 0))
    return records


def _children(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    # Rebuild the nesting from the parent links: roots in recording
    # order, and each record's children grouped under its seq. Under
    # threads, seq order can interleave trees, so exporters that need
    # tree order walk these rather than trusting the flat sequence.

    roots: list[dict[str, Any]] = []
    children: dict[int, list[dict[str, Any]]] = {}

    for record in records:
        parent = record.get("parent_id")
        if parent is None:
            roots.append(record)
        else:
            children.setdefault(parent, []).append(record)

    return roots, children


def load_events(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read a JSONLines trace file back into event records.

    Lines in the file are in completion order; the returned records
    are sorted by seq, recording order, ready to hand to an exporter.
    """

    with open(path, encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]

    records.sort(key=lambda record: record.get("seq", 0))
    return records


# The record fields that ride into a Chrome trace slice's args, where
# Perfetto shows them in the detail pane when a slice is clicked.

_SLICE_ARGS = (
    "seq",
    "parent_id",
    "arguments",
    "args",
    "kwargs",
    "forwarded",
    "result",
    "exception",
    "caught",
    "value",
    "previous",
    "items",
    "body_duration",
    "phase",
    "stack",
    "data",
)


def chrome_trace(source: TraceSource) -> str:
    """Render events as Chrome trace JSON for timeline viewers.

    The output opens directly in Perfetto (ui.perfetto.dev) and the
    older chrome://tracing: one lane per thread, one slice per event,
    nested slices for nested events, with the captured arguments,
    result and outcome shown in the detail pane when a slice is
    clicked. Timestamps are shifted so the earliest event starts at
    zero. An event that never closed becomes a begin-only slice, and
    a generator's slice spans creation to close, its accumulated body
    time riding along as body_duration.
    """

    records = _records(source)

    origin = min(
        (record["started"] for record in records if record.get("started") is not None),
        default=0.0,
    )

    # One metadata event names each thread lane; the process gets a
    # name too so a merged view stays identifiable.

    lanes: dict[int, str] = {}
    slices: list[dict[str, Any]] = []

    for record in records:
        started = record.get("started")
        if started is None:
            continue

        thread_id = record.get("thread_id", 0)
        thread_name = record.get("thread_name")
        if thread_name and thread_id not in lanes:
            lanes[thread_id] = thread_name

        entry: dict[str, Any] = {
            "name": record.get("label") or record["path"],
            "cat": record["kind"],
            "ph": "X",
            "ts": (started - origin) * 1e6,
            "pid": 1,
            "tid": thread_id,
            "args": {key: record[key] for key in _SLICE_ARGS if key in record},
        }

        duration = record.get("duration")
        if duration is not None:
            entry["dur"] = duration * 1e6
        else:
            entry["ph"] = "B"

        slices.append(entry)

    metadata: list[dict[str, Any]] = [
        {
            "ph": "M",
            "name": "process_name",
            "pid": 1,
            "args": {"name": "wrapture"},
        }
    ]
    for thread_id, thread_name in lanes.items():
        metadata.append(
            {
                "ph": "M",
                "name": "thread_name",
                "pid": 1,
                "tid": thread_id,
                "args": {"name": thread_name},
            }
        )

    return json.dumps({"traceEvents": metadata + slices}, ensure_ascii=False)


def canonical(source: TraceSource) -> str:
    """Render events as a canonical text tree for snapshot tests.

    One event per line, indented by nesting: the kind, the path, `!!`
    with the exception type for a failure, `(injected)` for an
    outcome supplied by behaviour rather than the real code, and one
    further `!!` marker per exception caught inside the scope and
    noted with note_exception(), as tree() draws them.
    Everything unstable between runs (sequence numbers, timings,
    captured values, thread identity) is left out, so the output is an
    architectural fingerprint: snapshot it once, and a change that
    silently alters what calls what fails the comparison in a diff a
    reviewer can read.
    """

    records = _records(source)
    roots, children = _children(records)

    lines: list[str] = []

    def emit(record: dict[str, Any], depth: int) -> None:
        line = "  " * depth + f"{record['kind']} {record['path']}"

        exception = record.get("exception")
        if exception is not None:
            line += f" !! {exception['type']}"

        if record.get("injected"):
            line += " (injected)"

        for caught in record.get("caught") or ():
            line += f" !! {caught['type']}"

        lines.append(line)

        for child in children.get(record["seq"], []):
            emit(child, depth + 1)

    for root in roots:
        emit(root, 0)

    return "\n".join(lines)


def _split_path(path: str) -> tuple[str, str]:
    # A path is module:attrpath; the container is everything up to the
    # final member. "mod:Class.method" belongs to "mod:Class", and a
    # module-level "mod:function" to "mod".

    module, _, attrpath = path.partition(":")

    if "." in attrpath:
        prefix, member = attrpath.rsplit(".", 1)
        return f"{module}:{prefix}", member

    return module, attrpath


def mermaid(source: TraceSource) -> str:
    """Render events as a Mermaid sequence diagram.

    Participants are the containers (classes and modules), messages
    are the members called on them in recorded order, with activation
    bars for nesting; a normal completion returns `return`, a failure
    returns the exception type, an exception caught inside the scope
    and noted with note_exception() follows the outcome as `!!` and
    its type (`return !! KeyError`), and attribute events carry their
    kind, such as `status (set)`. Mermaid renders natively on GitHub and in
    most documentation tooling, so the output drops straight into a
    pull request comment or a docs page. Best kept to small traces:
    sequence diagrams stop being readable beyond a few dozen events.
    """

    records = _records(source)
    roots, children = _children(records)

    # Participants are aliased P1, P2, ... because container names
    # contain colons and dots, which Mermaid syntax does not allow in
    # bare identifiers. Short display names are used unless two
    # containers share one, in which case both keep their full name.

    containers: list[str] = []
    for record in records:
        container = _split_path(record["path"])[0]
        if container not in containers:
            containers.append(container)

    short_names = [
        container.replace(":", ".").split(".")[-1] for container in containers
    ]
    displays = [
        short if short_names.count(short) == 1 else container
        for container, short in zip(containers, short_names, strict=True)
    ]
    aliases = {container: f"P{index + 1}" for index, container in enumerate(containers)}

    lines = ["sequenceDiagram", "    participant caller"]
    for container, display in zip(containers, displays, strict=True):
        lines.append(f"    participant {aliases[container]} as {display}")

    def message(record: dict[str, Any]) -> str:
        member = _split_path(record["path"])[1]

        if record["kind"] == "call":
            return member

        # A request reads as its request line when the details were
        # captured, falling back to the member-plus-kind form.

        if record["kind"] == "request":
            data = record.get("data") or {}
            method, path = data.get("method"), data.get("path")

            if method and path:
                return f"{method} {path}"

        return f"{member} ({record['kind']})"

    def emit(record: dict[str, Any], caller: str) -> None:
        callee = aliases[_split_path(record["path"])[0]]

        lines.append(f"    {caller}->>+{callee}: {message(record)}")

        for child in children.get(record["seq"], []):
            emit(child, callee)

        exception = record.get("exception")
        outcome = exception["type"] if exception is not None else "return"
        for caught in record.get("caught") or ():
            outcome += f" !! {caught['type']}"
        lines.append(f"    {callee}-->>-{caller}: {outcome}")

    for root in roots:
        emit(root, "caller")

    return "\n".join(lines)
