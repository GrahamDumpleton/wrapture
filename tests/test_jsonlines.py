"""Tests for the JSONLines sink and the serialised event form.

One JSON object per line, written when an event closes, so lines
appear in completion order and carry the outcome; identity fields are
always present, everything else only when observed, keeping an absent
"result" distinguishable from "result": null. The observed
application is never blocked: a bounded queue feeds a background
writer, drops are counted, and flush()/close() put queued lines on
disk.
"""

import json
import os
import threading
import time
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import wrapture
from wrapture import ConfigWarning, JSONLines, Sink, binding, load_events
from wrapture.sinks import _scoped_sinks


class Gateway:
    def charge(self, amount: int, currency: str = "USD") -> str:
        return f"ch_{amount}"

    def refund(self, amount: int) -> str:
        raise TimeoutError("gateway offline")

    def nothing(self) -> None:
        return None


class Dispatcher:
    def submit(self, item: str, **options: Any) -> None:
        return None


class Processor:
    def process(self) -> str:
        return Gateway().charge(500)


@contextmanager
def listening(*sinks: Sink) -> Generator[None]:
    token = _scoped_sinks.set(_scoped_sinks.get() + sinks)
    try:
        yield
    finally:
        _scoped_sinks.reset(token)


def read_lines(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


# ---------------------------------------------------------------------------
# the serialised form
# ---------------------------------------------------------------------------


def test_each_completed_event_becomes_one_json_line(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)

    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")

    with process, charge, listening(sink):
        Processor().process()

    sink.close()
    lines = read_lines(trace)

    # Completion order: the nested charge closes before the processor
    # that called it; sorting by seq recovers recording order, and
    # parent_id rebuilds the nesting.

    # An unnamed binding records label None, and the record omits the
    # key entirely; the path is the identity.

    assert all("label" not in line for line in lines)
    assert [line["path"] for line in lines] == [
        "test_jsonlines:Gateway.charge",
        "test_jsonlines:Processor.process",
    ]

    by_seq = sorted(lines, key=lambda line: line["seq"])
    outer, inner = by_seq

    assert outer["parent_id"] is None
    assert outer["depth"] == 0
    assert inner["parent_id"] == outer["seq"]
    assert inner["depth"] == 1

    assert inner["kind"] == "call"
    assert inner["path"].endswith(":Gateway.charge")
    assert inner["arguments"] == {"amount": 500, "currency": "USD"}
    assert inner["result"] == "ch_500"
    assert inner["duration"] >= 0.0
    assert inner["started"] > 0.0

    # Thread identity is on every line, captured where the operation
    # began, so lines can be grouped by lane after the fact.

    assert inner["thread_id"] == threading.get_ident()
    assert inner["thread_name"] == threading.current_thread().name


def test_var_keyword_name_rides_the_line_and_survives_reload(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)

    submit = binding(Dispatcher, "submit")
    charge = binding(Gateway, "charge")

    with submit, charge, listening(sink):
        Dispatcher().submit("job", parent_id="id-1")
        Gateway().charge(500)

    sink.close()

    from wrapture import load_events

    submitted, charged = load_events(trace)

    # The bundle parameter's name is on the line when the target has
    # one, so a reloaded record still says where the extra keywords
    # live (under this sink's summary capture, as the bundle's summary);
    # a target without one carries no such field.

    assert submitted["var_keyword"] == "options"
    assert "parent_id" in submitted["arguments"]["options"]
    assert "var_keyword" not in charged


def test_a_raising_call_serialises_its_exception(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)
    refund = binding(Gateway, "refund")

    with refund, listening(sink):
        with pytest.raises(TimeoutError):
            Gateway().refund(100)

    sink.close()
    (line,) = read_lines(trace)

    assert line["exception"] == {"type": "TimeoutError", "message": "gateway offline"}
    assert "result" not in line
    assert line["duration"] >= 0.0


def test_a_noted_exception_serialises_with_its_offset_and_reloads(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)
    process = binding(Processor, "process")
    handled = TimeoutError("gateway offline")

    def noting(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        result = wrapped(*args, **kwargs)
        wrapture.note_exception(handled)
        return result

    process.on_call.decorates(noting)

    with process, listening(sink):
        Processor().process()

    sink.close()
    (line,) = [line for line in read_lines(trace) if line["path"].endswith("process")]

    # The note rides the line as type, message and the seconds since
    # the event started; there is no escaped exception.

    assert "exception" not in line
    (caught,) = line["caught"]
    assert caught["type"] == "TimeoutError"
    assert caught["message"] == "gateway offline"
    assert 0.0 <= caught["offset"] <= line["duration"]

    # The reloaded record carries it unchanged, for the exporters.

    (record,) = [r for r in load_events(trace) if r["path"].endswith("process")]
    assert record["caught"] == line["caught"]


def test_returned_none_stays_distinguishable_from_nothing_captured(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)

    nothing = binding(Gateway, "nothing")
    silent = binding(Gateway, "charge", capture="none")

    with nothing, silent, listening(sink):
        gateway = Gateway()
        gateway.nothing()
        gateway.charge(500)

    sink.close()
    returned_none, uncaptured = read_lines(trace)

    # "result": null is a call that returned None; no "result" key at
    # all is a call whose values were not captured.

    assert returned_none["result"] is None
    assert "result" not in uncaptured
    assert "arguments" not in uncaptured


def test_values_captured_by_reference_are_reduced_at_serialisation(
    tmp_path: Path,
) -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "<an open connection>"

    class Registry:
        def lookup(self, key: str) -> Opaque:
            return Opaque()

    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)

    # A binding override forces reference capture despite the sink's
    # "summary" declaration, so the event holds a live object; the
    # line still serialises, reduced to a bounded string.

    lookup = binding(Registry, "lookup", capture="reference")

    with lookup, listening(sink):
        Registry().lookup("db")

    sink.close()
    (line,) = read_lines(trace)

    assert isinstance(line["result"], str)
    assert "connection" in line["result"]


def test_self_referential_values_cannot_hang_serialisation(tmp_path: Path) -> None:
    class Builder:
        def loop(self) -> list[Any]:
            cycle: list[Any] = []
            cycle.append(cycle)
            return cycle

    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)
    loop = binding(Builder, "loop", capture="reference")

    with loop, listening(sink):
        Builder().loop()

    sink.close()
    (line,) = read_lines(trace)

    # The depth bound cuts the cycle and reports only types past it.

    assert "<list>" in json.dumps(line["result"])


# ---------------------------------------------------------------------------
# never block, never lose silently
# ---------------------------------------------------------------------------


def test_a_full_queue_drops_and_counts_instead_of_blocking(tmp_path: Path) -> None:
    sink = JSONLines(tmp_path / "trace.jsonl", limit=2)

    # Keep the writer from ever starting, so the queue can only fill:
    # the observed calls must still complete instantly, with the
    # overflow counted rather than waited on.

    sink._started = True

    charge = binding(Gateway, "charge")

    with charge, listening(sink):
        gateway = Gateway()
        for amount in range(5):
            gateway.charge(amount)

    assert sink.dropped == 3


def test_close_drains_the_queue_and_further_events_are_counted(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)
    charge = binding(Gateway, "charge")

    with charge, listening(sink):
        Gateway().charge(1)
        sink.close()
        Gateway().charge(2)

    assert [line["arguments"]["amount"] for line in read_lines(trace)] == [1]
    assert sink.dropped == 1

    # close() is idempotent.

    sink.close()


def test_flush_puts_queued_lines_on_disk_without_closing(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace)
    charge = binding(Gateway, "charge")

    with charge, listening(sink):
        Gateway().charge(1)
        sink.flush()

        assert len(read_lines(trace)) == 1

        Gateway().charge(2)
        sink.flush()

        assert len(read_lines(trace)) == 2

    sink.close()


def test_an_unopenable_path_breaks_the_sink_not_the_application(
    tmp_path: Path,
) -> None:
    # A missing directory is created on open, so the unopenable case
    # is a path whose parent is an existing plain file.

    (tmp_path / "not-a-directory").write_text("")
    sink = JSONLines(tmp_path / "not-a-directory" / "trace.jsonl")
    charge = binding(Gateway, "charge")

    with charge, listening(sink):
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")

            assert Gateway().charge(500) == "ch_500"
            sink.flush()

    sink.close()
    assert sink.errors >= 1


def test_reopen_moves_on_to_the_file_the_template_names_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rotation is wrapture's own: reopen() expands the template again,
    # so a time variable in the path names a fresh file, with queued
    # lines draining to the old one first. The clock is injected so
    # the two expansions differ deterministically.

    import wrapture.outputs

    clock = [1_700_000_000.0]
    monkeypatch.setattr(wrapture.outputs, "_now", lambda: clock[0])

    sink = JSONLines(tmp_path / "trace-{epoch}.jsonl")
    charge = binding(Gateway, "charge")

    with charge, listening(sink):
        Gateway().charge(1)
        sink.flush()

        clock[0] += 3600
        sink.reopen()

        Gateway().charge(2)

    sink.close()

    first = tmp_path / "trace-1700000000.jsonl"
    second = tmp_path / "trace-1700003600.jsonl"

    assert [line["arguments"]["amount"] for line in read_lines(first)] == [1]
    assert [line["arguments"]["amount"] for line in read_lines(second)] == [2]


def test_the_path_creates_its_directories_and_reports_where_it_writes(
    tmp_path: Path,
) -> None:
    sink = JSONLines(tmp_path / "traces" / "{name}" / "trace-{pid}.jsonl", name="ops")
    charge = binding(Gateway, "charge")

    assert sink.path is None
    assert repr(sink).endswith("trace-{pid}.jsonl')")

    with charge, listening(sink):
        Gateway().charge(1)
        sink.flush()

    sink.close()

    expected = tmp_path / "traces" / "ops" / f"trace-{os.getpid()}.jsonl"

    assert sink.path == str(expected)
    assert f"writing={str(expected)!r}" in repr(sink)
    assert [line["arguments"]["amount"] for line in read_lines(expected)] == [1]


def test_an_unknown_path_variable_fails_at_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown variable {seq}"):
        JSONLines(tmp_path / "trace-{seq}.jsonl")

    # Window variables are accepted; they fail at first open when no
    # window has supplied a context, counted as a sink error.

    sink = JSONLines(tmp_path / "trace-{run}.jsonl")
    charge = binding(Gateway, "charge")

    with charge, listening(sink):
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            Gateway().charge(1)
            sink.flush()

    sink.close()

    assert sink.errors >= 1


def test_rotate_on_an_untimed_path_warns_and_align_needs_rotate(
    tmp_path: Path,
) -> None:
    with pytest.warns(ConfigWarning, match="no time variable"):
        JSONLines(tmp_path / "trace.jsonl", rotate="1h")

    with pytest.raises(ValueError, match="align=True needs a rotate"):
        JSONLines(tmp_path / "trace.jsonl", align=True)

    with pytest.raises(ValueError, match="not a number with a unit"):
        JSONLines(tmp_path / "trace-{time}.jsonl", rotate="soon")


def test_rotate_reopens_on_its_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A short interval on the real scheduler: once the clock has moved
    # on, the next firing reopens to the new name and the next line
    # lands in the new file.

    import wrapture.outputs

    clock = [1_700_000_000.0]
    monkeypatch.setattr(wrapture.outputs, "_now", lambda: clock[0])

    sink = JSONLines(tmp_path / "trace-{epoch}.jsonl", rotate=0.05)
    charge = binding(Gateway, "charge")

    with charge, listening(sink):
        Gateway().charge(1)
        sink.flush()

        # The timer started with the first line and may already have
        # fired, reopening the same file under the old clock, so wait
        # for the observable outcome rather than a firing count: the
        # sink reporting that it now writes the new name.

        clock[0] += 1
        deadline = time.monotonic() + 5
        while sink.path != str(tmp_path / "trace-1700000001.jsonl"):
            assert time.monotonic() < deadline, "rotation never fired"
            time.sleep(0.01)

        Gateway().charge(2)

    sink.close()

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "trace-1700000000.jsonl",
        "trace-1700000001.jsonl",
    ]

    first = read_lines(tmp_path / "trace-1700000000.jsonl")
    second = read_lines(tmp_path / "trace-1700000001.jsonl")
    assert [line["arguments"]["amount"] for line in first] == [1]
    assert [line["arguments"]["amount"] for line in second] == [2]


def test_type_level_drops_exception_messages(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    sink = JSONLines(trace, exceptions="type")
    refund = binding(Gateway, "refund")

    with refund, listening(sink):
        with pytest.raises(TimeoutError):
            Gateway().refund(100)

    sink.close()
    (line,) = read_lines(trace)

    assert line["exception"] == {"type": "TimeoutError"}

    with pytest.raises(ValueError, match="exceptions must be one of"):
        JSONLines(trace, exceptions="short")
