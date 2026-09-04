"""Tests for capture policies, injected-result marking and annotation.

Capture decides how much of a call's values the tape stores. The tests
pin the level semantics (what each stores, what each never touches), the
binding override versus the tape's declared requirement, redaction, the
injected mark, and the annotate()/current_event() escape hatch.
"""

import threading
from typing import Any

from wrapt import MISSING

from wrapture import (
    annotate,
    binding,
    capture_query,
    current_event,
    redact,
    timeline,
)
from wrapture.capture import NONE, summarize, type_name


class Ledger:
    def record(self, entries: list[str]) -> int:
        return len(entries)


class Vault:
    def open(self, secret: str, attempts: int = 1) -> str:
        return f"opened:{secret}"


# ---------------------------------------------------------------------------
# levels
# ---------------------------------------------------------------------------


def test_reference_default_stores_references() -> None:
    record = binding(Ledger, "record")
    entries = ["a", "b"]

    with timeline(record):
        Ledger().record(entries)

        event = record.events.first
        assert event.arguments is not None
        assert event.arguments["entries"] is entries

        # The honest downside of REFERENCE, and why other levels exist:
        # mutation after the call shows up in the record.

        entries.append("c")
        assert event.arguments["entries"] == ["a", "b", "c"]


def test_summary_snapshots_a_bounded_repr() -> None:
    record = binding(Ledger, "record", capture="summary")
    entries = ["a", "b"]

    with timeline(record):
        Ledger().record(entries)

        event = record.events.first
        entries.append("c")

        assert event.arguments is not None
        assert event.arguments["entries"] == "<list ['a', 'b']>"
        assert event.args is None
        assert event.result == 2


def test_types_records_type_names_only() -> None:
    record = binding(Ledger, "record", capture="types")

    with timeline(record):
        Ledger().record(["a"])

        event = record.events.first
        assert event.arguments is not None
        assert event.arguments["entries"] == "<list>"
        assert event.result == "<int>"


def test_none_keeps_the_event_but_no_values() -> None:
    record = binding(Ledger, "record", capture="none")

    with timeline(record):
        Ledger().record(["a"])

        event = record.events.assert_once().first
        assert event.args is None
        assert event.arguments is None
        assert event.result is MISSING
        assert event.capture == NONE


def test_snapshot_deepcopies_and_falls_back_where_it_cannot() -> None:
    record = binding(Ledger, "record", capture="snapshot")
    entries = ["a", "b"]

    with timeline(record):
        Ledger().record(entries)
        Ledger().record([threading.Lock()])  # type: ignore[list-item]

        copied, locked = record.events

        entries.append("c")
        assert copied.arguments is not None
        assert copied.arguments["entries"] == ["a", "b"]

        # deepcopy raises on a lock; the fallback is the bounded repr,
        # never a failure of the call under test.

        assert locked.arguments is not None
        assert isinstance(locked.arguments["entries"], str)


def test_specific_axis_wins_over_the_shorthand() -> None:
    record = binding(Ledger, "record", capture="none", capture_result="summary")

    with timeline(record):
        Ledger().record(["a"])

        event = record.events.first
        assert event.arguments is None
        assert event.result == 1


# ---------------------------------------------------------------------------
# callable policies and redaction
# ---------------------------------------------------------------------------


def test_redact_matches_by_name_however_the_caller_passed_it() -> None:
    vault = binding(Vault, "open", capture_args=redact("secret"))

    with timeline(vault):
        Vault().open("hunter2")
        Vault().open(secret="hunter2", attempts=3)

        for event in vault.events:
            assert event.arguments is not None
            assert event.arguments["secret"] == "<redacted>"

        # Unnamed parameters are captured normally, and the result is
        # not touched: redaction is by parameter name only.

        assert vault.events.last.arguments["attempts"] == 3  # type: ignore[index]
        assert vault.events.first.result == "opened:hunter2"


def test_a_misspelled_capture_level_raises_at_creation() -> None:
    import pytest

    with pytest.raises(ValueError, match="capture level must be"):
        binding(Ledger, "record", capture="sumary")


def test_redact_accepts_a_level_name_for_everything_unnamed() -> None:
    vault = binding(Vault, "open", capture_args=redact("secret", level="types"))

    with timeline(vault):
        Vault().open("hunter2", attempts=3)

        event = vault.events.first
        assert event.arguments is not None
        assert event.arguments["secret"] == "<redacted>"
        assert event.arguments["attempts"] == "<int>"


def test_a_custom_callable_is_a_policy() -> None:
    def masked(name: str | None, value: Any) -> Any:
        return f"{name}!{value}"

    record = binding(Ledger, "record", capture_args=masked)

    with timeline(record):
        Ledger().record(["a"])

        event = record.events.first
        assert event.arguments == {"entries": "entries!['a']"}


# ---------------------------------------------------------------------------
# injected results
# ---------------------------------------------------------------------------


def test_injected_results_are_marked_not_suppressed() -> None:
    record = binding(Ledger, "record").on_call.returns(99)

    with timeline(record) as tape:
        Ledger().record(["a"])

        event = record.events.first
        assert event.result == 99
        assert event.injected

        assert record.events.injected().count == 1
        assert record.events.injected(False).count == 0
        assert "-> 99 (injected)" in tape.tree()


def test_a_real_result_is_not_marked() -> None:
    record = binding(Ledger, "record")

    with timeline(record):
        Ledger().record(["a"])

        assert not record.events.first.injected
        assert record.events.injected(False).count == 1


def test_passes_through_clears_the_injected_mark() -> None:
    record = binding(Ledger, "record").on_call.returns(99)
    record.on_call.passes_through()

    with timeline(record):
        Ledger().record(["a"])

        assert not record.events.first.injected


# ---------------------------------------------------------------------------
# annotation
# ---------------------------------------------------------------------------


def test_annotate_lands_on_the_in_flight_event() -> None:
    def around(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        annotate(item_count=len(args[0]), items=tuple(args[0]))
        return wrapped(*args, **kwargs)

    record = binding(Ledger, "record").on_call.decorates(around)
    entries = ["a", "b"]

    with timeline(record):
        Ledger().record(entries)

        entries.append("c")
        event = record.events.first

        # The annotation is the caller's own immutable copy, unaffected
        # by later mutation, alongside the by-reference record.

        assert event.data == {"item_count": 2, "items": ("a", "b")}


def test_annotations_filter_like_anything_else() -> None:
    def around(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        annotate(rows=len(args[0]))
        return wrapped(*args, **kwargs)

    record = binding(Ledger, "record").on_call.decorates(around)

    with timeline(record):
        Ledger().record(["a"])
        Ledger().record(["a", "b", "c"])

        big = record.events.matching(lambda e: e.data.get("rows", 0) > 1)
        assert big.count == 1


def test_annotate_outside_recording_is_a_silent_no_op() -> None:
    assert not current_event()
    annotate(rows=1)


def test_current_event_names_the_innermost_call() -> None:
    seen: list[str] = []

    def around(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        event = current_event()
        assert event
        seen.append(event.label or event.path)
        return wrapped(*args, **kwargs)

    record = binding(Ledger, "record").on_call.decorates(around)

    with timeline(record):
        Ledger().record(["a"])

    assert seen == ["test_capture:Ledger.record"]


# ---------------------------------------------------------------------------
# the building blocks
# ---------------------------------------------------------------------------


def test_type_name_touches_only_the_class() -> None:
    class Loud:
        def __repr__(self) -> str:
            raise RuntimeError("repr has side effects")

    assert type_name(Loud()) == "<Loud>"


def test_summarize_bounds_work_and_never_raises() -> None:
    class Unreprable:
        def __repr__(self) -> str:
            raise RuntimeError("no")

    assert summarize("x" * 500) == "x" * 200 + "...+300"
    assert summarize(list(range(100))).startswith("<list [0, 1, ")
    assert summarize({"k": "v"}) == "<dict {'k': 'v'}>"
    assert summarize(42) == 42
    assert summarize(Unreprable()) == "<unreprable Unreprable: RuntimeError>"


# ---------------------------------------------------------------------------
# capture_query: the recorded form of a query string
# ---------------------------------------------------------------------------


def test_capture_query_redacts_the_sensitive_names_by_default() -> None:
    recorded = capture_query(
        "limit=5&access_token=sekrit&ApiKey=k1&PHPSESSID=abc&X-Amz-Signature=s1"
    )

    assert recorded == (
        "limit=5&access_token=<redacted>&ApiKey=<redacted>"
        "&PHPSESSID=<redacted>&X-Amz-Signature=<redacted>"
    )


def test_capture_query_takes_redact_names_on_top_of_the_built_in_set() -> None:
    recorded = capture_query("signature=abc&limit=5&voucher=SAVE10", redact("voucher"))

    assert recorded == "signature=<redacted>&limit=5&voucher=<redacted>"


def test_capture_query_records_the_decoded_display_form() -> None:
    assert capture_query("q=hello+world&name=p%C3%A4t") == "q=hello world&name=pät"
    assert capture_query("flag&empty=") == "flag=&empty="


def test_capture_query_applies_the_level_to_what_is_not_redacted() -> None:
    # "types" reduces every value to its type name; the sensitive set
    # still wins over it.

    assert capture_query("limit=5&token=t", "types") == "limit=<str>&token=<redacted>"


def test_capture_query_records_the_marker_wholesale_when_it_cannot_process() -> None:
    def broken(name: str | None, value: Any) -> Any:
        raise RuntimeError("no")

    assert capture_query("a=1&b=2", broken) == "<redacted>"


def test_a_call_through_the_class_normalises_its_arguments_like_an_instance_call() -> (
    None
):
    # Gateway.charge(gateway, 5) and gateway.charge(5) bind the same
    # way: the wrapt partial the class-call path hands over reports a
    # signature without self (wrapt 2.4.1), so self is never mistaken
    # for the first parameter.

    class Gateway:
        def charge(self, amount: int, currency: str = "USD") -> str:
            return f"ch_{amount}"

    charge = binding(Gateway, "charge")
    gateway = Gateway()

    with timeline(charge) as tape:
        gateway.charge(5)
        Gateway.charge(gateway, 5)

    direct, through_class = tape.all
    assert direct.arguments == {"amount": 5, "currency": "USD"}
    assert through_class.arguments == direct.arguments
