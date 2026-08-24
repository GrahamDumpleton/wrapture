"""Tests for the built-in collectors, Counter and Aggregate, as the
window contents that report at run close."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from wrapture import (
    Aggregate,
    Collector,
    ConfigError,
    Counter,
    Filter,
    Window,
    binding,
    load_config,
    window,
)


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"

    def refund(self, amount: int) -> str:
        raise TimeoutError("gateway timed out")


class Processor:
    def process(self) -> str:
        Gateway().charge(1)
        return "ok"


def test_counter_reports_the_count_and_resets() -> None:
    counter = Counter()
    charge = binding(Gateway, "charge")

    assert isinstance(counter, Collector)

    with charge, window(collect=[counter]) as run:
        Gateway().charge(1)
        Gateway().charge(2)

    (report,) = run.reports

    assert report.kind == "counter"
    assert report.name == "counter"
    assert report.data == {"count": 2}
    assert report.text.startswith('counter "counter" run 1, ')
    assert report.text.endswith("\n2 operations\n")
    assert "pid" in report.text

    # Reset for the next run; the report kept its number.

    assert counter.count == 0


def test_aggregate_reports_a_table_sorted_by_self_time() -> None:
    aggregate = Aggregate(name="hot")
    process = binding(Processor, "process")
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")

    with process, charge, refund, window(name="stats", collect=[aggregate]) as run:
        Processor().process()
        Processor().process()
        with pytest.raises(TimeoutError):
            Gateway().refund(3)

    (report,) = run.reports
    lines = report.text.splitlines()

    assert report.kind == "aggregate"
    assert report.name == "hot"
    assert lines[0].startswith('aggregate "hot" run 1, ')
    assert lines[1] == "3 paths, 5 operations begun, 5 completed, 1 raised"
    assert lines[2] == ""
    assert lines[3].split() == [
        "calls",
        "total",
        "self",
        "per-call",
        "min",
        "max",
        "errors",
        "path",
    ]

    charge_path = f"{__name__}:Gateway.charge"
    refund_path = f"{__name__}:Gateway.refund"
    process_path = f"{__name__}:Processor.process"

    # One row per path, in the data's (and table's) order: self time
    # descending, and every figure present.

    paths = report.data["paths"]
    assert set(paths) == {charge_path, refund_path, process_path}
    selfs = [row["self"] for row in paths.values()]
    assert selfs == sorted(selfs, reverse=True)

    assert paths[charge_path]["count"] == 2
    assert paths[charge_path]["errors"] == 0
    assert paths[refund_path]["errors"] == 1
    assert paths[process_path]["self"] < paths[process_path]["total"]
    assert report.data["begun"] == 5
    assert report.data["raised"] == 1

    # The refund row shows its error count in the errors column.

    refund_line = next(line for line in lines if line.endswith(refund_path))
    assert refund_line.split()[-2] == "1"

    # Reset clears everything for the next run.

    assert aggregate.stats == {}
    assert aggregate._pending == {}


def test_aggregate_omits_the_errors_column_when_nothing_raised() -> None:
    aggregate = Aggregate()
    charge = binding(Gateway, "charge")

    with charge, window(collect=[aggregate]) as run:
        Gateway().charge(1)

    (report,) = run.reports
    header = report.text.splitlines()[3]

    assert "errors" not in header
    assert report.data["raised"] == 0


def test_path_stats_carry_completion_and_error_counts() -> None:
    aggregate = Aggregate()
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")

    with charge, refund, window(collect=[aggregate]):
        Gateway().charge(1)
        with pytest.raises(TimeoutError):
            Gateway().refund(1)

        stats = aggregate.stats
        charged = stats[f"{__name__}:Gateway.charge"]
        refunded = stats[f"{__name__}:Gateway.refund"]

        assert (charged.count, charged.completed, charged.errors) == (1, 1, 0)
        assert (refunded.count, refunded.completed, refunded.errors) == (1, 1, 1)
        assert charged.per_call == charged.total


def test_a_gated_collector_arms_and_reports_hearing_only_what_passes() -> None:
    aggregate = Aggregate()
    only_charges = Filter(lambda event: event.path.endswith("charge"), aggregate)
    charge = binding(Gateway, "charge")
    refund = binding(Gateway, "refund")

    span = Window(name="gated", collect=[only_charges])
    assert span.collectors == (aggregate,)

    span.start()
    try:
        with charge, refund:
            Gateway().charge(1)
            with pytest.raises(TimeoutError):
                Gateway().refund(1)
    finally:
        span.stop()

    (report,) = span.reports
    assert list(report.data["paths"]) == [f"{__name__}:Gateway.charge"]


def test_config_names_collectors_only_under_a_window(tmp_path: Path) -> None:
    source = tmp_path / "trace.toml"
    source.write_text(
        textwrap.dedent(
            """
            [[window]]
            name = "stats"
            report = "reports/{window}-{run}.txt"

            [[window.collect]]
            type = "aggregate"
            name = "hot"

            [[window.collect]]
            type = "counter"
            filter = { kind = "request" }
            """
        )
    )

    (span,) = load_config(source).windows
    aggregate, counter = span.collectors

    assert isinstance(aggregate, Aggregate)
    assert aggregate.name == "hot"
    assert isinstance(counter, Counter)
    assert type(span.sinks).__name__ == "tuple" and span.sinks == ()

    source.write_text('[[sink]]\ntype = "aggregate"\n')

    with pytest.raises(ConfigError, match=r"put it under \[\[window.collect\]\]"):
        load_config(source)

    source.write_text('[[window]]\nname = "x"\n[[window.collect]]\ntype = "nope"\n')

    with pytest.raises(ConfigError, match="not a builtin sink or collector"):
        load_config(source)


class Shop:
    """The framework shape: dispatch() catches what the view raises,
    hands it to handle_error(), and returns normally."""

    def dispatch(self, sku: str) -> str:
        try:
            return Gateway().refund(1)
        except TimeoutError as exc:
            return self.handle_error(exc)

    def handle_error(self, exc: BaseException) -> str:
        return "500"


def test_aggregate_counts_a_noted_exception_as_an_error() -> None:
    import wrapture

    aggregate = Aggregate()
    dispatch = binding(Shop, "dispatch")

    def noting(wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: Any) -> str:
        wrapture.current_event(binding=dispatch).note_exception(args[0])
        return "500"

    handle = binding(Shop, "handle_error").on_call.decorates(noting)

    with dispatch, handle, window(name="stats", collect=[aggregate]) as run:
        assert Shop().dispatch("sku") == "500"

    (report,) = run.reports
    dispatch_path = f"{__name__}:Shop.dispatch"
    handle_path = f"{__name__}:Shop.handle_error"

    # The dispatch completed with a result, and still counts as the
    # one failure; the handler itself, which the note was aimed past,
    # does not.

    assert report.text.splitlines()[1] == (
        "2 paths, 2 operations begun, 2 completed, 1 raised"
    )
    assert report.data["paths"][dispatch_path]["completed"] == 1
    assert report.data["paths"][dispatch_path]["errors"] == 1
    assert report.data["paths"][handle_path]["errors"] == 0
    assert report.data["raised"] == 1
