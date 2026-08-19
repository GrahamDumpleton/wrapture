"""Tests for phased behaviour: then(), advance(), phase, and the phase
stamp on events."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from wrapture import (
    CallBehaviour,
    CallPhase,
    GetPhase,
    binding,
    timeline,
)


class Gateway:
    # Returns Any so tests can compare stubbed values of other types.

    def charge(self, amount: int, currency: str = "USD") -> Any:
        return {"id": f"ch_{amount}", "amount": amount}


class Settings:
    beta_enabled = False
    retries = 3


# ---------------------------------------------------------------------------
# then(): namespaces and the chain
# ---------------------------------------------------------------------------


def test_base_namespace_verbs_return_the_binding_and_phase_verbs_the_phase() -> None:
    charge = binding(Gateway, "charge")

    assert isinstance(charge.on_call, CallBehaviour)
    assert charge.on_call.returns(1) is charge

    later = charge.on_call.then(after=1)
    assert isinstance(later, CallPhase)
    assert later.returns(2) is later
    assert later.transforms_result(lambda r: r) is later


def test_then_is_the_same_successor_each_time() -> None:
    charge = binding(Gateway, "charge")

    first = charge.on_call.then(after=2)
    again = charge.on_call.then()

    assert first._phase is again._phase
    assert charge.on_call.then(after=2)._phase is first._phase


def test_then_repeat_with_argument_replaces_the_exit_and_bare_repeat_keeps_it() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")

    later = charge.on_call.then(after=5)
    later.returns("b")

    charge.on_call.then()  # bare repeat: exit still after=5
    charge.on_call.then(after=1)  # replaces: now after=1

    with charge:
        gateway = Gateway()
        assert gateway.charge(1) == "a"
        assert gateway.charge(1) == "b"


def test_then_rejects_both_conditions_and_bad_counts() -> None:
    charge = binding(Gateway, "charge")

    with pytest.raises(TypeError, match="not both"):
        charge.on_call.then(after=1, until=lambda e: True)

    with pytest.raises(ValueError, match="positive int"):
        charge.on_call.then(after=0)

    with pytest.raises(ValueError, match="positive int"):
        charge.on_call.then(after=True)


def test_successor_of_a_successor_is_relative_to_that_phase() -> None:
    lookup = binding(Gateway, "charge")
    lookup.on_call.returns("a")

    second = lookup.on_call.then(after=1)
    second.returns("b")

    third = second.then(after=2)
    third.returns("c")

    with lookup:
        gateway = Gateway()
        seen = [gateway.charge(1) for _ in range(5)]

    assert seen == ["a", "b", "b", "c", "c"]
    assert lookup.phase == 2


# ---------------------------------------------------------------------------
# count exits, phase and advance()
# ---------------------------------------------------------------------------


def test_after_n_hands_over_after_exactly_n_calls_and_the_last_phase_stays() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.raises(TimeoutError("down"))

    recovered = charge.on_call.then(after=2)
    recovered.passes_through()

    with charge:
        gateway = Gateway()
        assert charge.phase == 0

        for _ in range(2):
            with pytest.raises(TimeoutError):
                gateway.charge(1)

        assert charge.phase == 1
        assert gateway.charge(1) == {"id": "ch_1", "amount": 1}
        assert gateway.charge(2) == {"id": "ch_2", "amount": 2}
        assert charge.phase == 1


def test_a_phase_with_no_verbs_passes_through() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("stub")
    charge.on_call.then(after=1)

    with charge:
        gateway = Gateway()
        assert gateway.charge(1) == "stub"
        assert gateway.charge(1) == {"id": "ch_1", "amount": 1}


def test_phases_are_independent_nothing_is_inherited() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.transforms_result(lambda r: "transformed")
    charge.on_call.returns("stub")

    later = charge.on_call.then(after=1)
    later.returns("other")

    with charge:
        gateway = Gateway()
        assert gateway.charge(1) == "transformed"
        assert gateway.charge(1) == "other"


def test_a_second_terminal_on_a_phase_replaces_the_first() -> None:
    charge = binding(Gateway, "charge")
    later = charge.on_call.then(after=1)
    later.returns("b").raises(KeyError("no"))

    with charge:
        gateway = Gateway()
        gateway.charge(1)
        with pytest.raises(KeyError):
            gateway.charge(1)


def test_manual_phase_ends_only_on_advance() -> None:
    remote = binding(Gateway, "charge")
    remote.on_call.raises(ConnectionError("down"))

    online = remote.on_call.then()
    online.passes_through()

    with remote:
        gateway = Gateway()
        for _ in range(3):
            with pytest.raises(ConnectionError):
                gateway.charge(1)

        assert remote.advance() is remote
        assert remote.phase == 1
        assert gateway.charge(1)["id"] == "ch_1"


def test_advance_forces_a_count_phase_early_and_is_a_noop_at_the_end() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")
    charge.on_call.then(after=10).returns("b")

    with charge:
        gateway = Gateway()
        assert gateway.charge(1) == "a"

        charge.advance()
        assert gateway.charge(1) == "b"

        charge.advance()
        charge.advance()
        assert charge.phase == 1
        assert gateway.charge(1) == "b"


def test_phase_and_advance_on_an_unphased_binding() -> None:
    charge = binding(Gateway, "charge")
    assert charge.phase == 0
    assert charge.advance() is charge
    assert charge.phase == 0

    charge.on_call.returns("stub")
    assert charge.phase == 0


def test_apply_restarts_from_phase_zero_but_suspend_and_resume_do_not() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")
    charge.on_call.then(after=1).returns("b")

    gateway = Gateway()

    with charge:
        assert gateway.charge(1) == "a"
        assert gateway.charge(1) == "b"

        charge.suspend()
        gateway.charge(1)  # real call, not counted
        charge.resume()
        assert charge.phase == 1
        assert gateway.charge(1) == "b"

    with charge:
        assert charge.phase == 0
        assert gateway.charge(1) == "a"


def test_behaviour_only_and_filtered_calls_still_count() -> None:
    quiet = binding(Gateway, "charge", when=False)
    quiet.on_call.returns("a")
    quiet.on_call.then(after=1).returns("b")

    with quiet:
        gateway = Gateway()
        assert gateway.charge(1) == "a"
        assert gateway.charge(1) == "b"

    filtered = binding(Gateway, "charge", when=lambda i, a, k: False)
    filtered.on_call.returns("a")
    filtered.on_call.then(after=1).returns("b")

    with filtered, timeline() as tape:
        gateway = Gateway()
        assert gateway.charge(1) == "a"
        assert gateway.charge(1) == "b"

    assert len(tape.all) == 0


def test_passes_through_on_a_phase_clears_only_that_phase() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")

    later = charge.on_call.then(after=1)
    later.returns("b")
    later.passes_through()

    with charge:
        gateway = Gateway()
        assert gateway.charge(1) == "a"
        assert gateway.charge(1)["id"] == "ch_1"


def test_a_phase_namespace_can_advance_and_report_its_operation() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")
    charge.on_call.then().returns("b")

    with charge:
        gateway = Gateway()
        assert charge.on_call.phase == 0
        charge.on_call.advance()
        assert charge.on_call.phase == 1
        assert gateway.charge(1) == "b"


# ---------------------------------------------------------------------------
# attribute bindings
# ---------------------------------------------------------------------------


def test_attribute_reads_flip_after_three_reads() -> None:
    flag = binding(Settings, "beta_enabled")
    flag.on_get.returns(False)

    enabled = flag.on_get.then(after=3)
    assert isinstance(enabled, GetPhase)
    enabled.returns(True)

    with flag:
        settings = Settings()
        assert [settings.beta_enabled for _ in range(5)] == [
            False,
            False,
            False,
            True,
            True,
        ]

    assert flag.phase == 1


def test_binding_phase_is_ambiguous_across_operations() -> None:
    flag = binding(Settings, "beta_enabled")
    flag.on_get.then(after=1)
    flag.on_set.then(after=1)

    with pytest.raises(ValueError, match="on_get.phase"):
        _ = flag.phase

    assert flag.on_get.phase == 0
    assert flag.on_set.phase == 0

    # advance() still moves every phased operation on

    flag.advance()
    assert flag.on_get.phase == 1
    assert flag.on_set.phase == 1


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_events_carry_the_phase_only_when_the_binding_is_phased() -> None:
    plain = binding(Gateway, "charge")
    plain.on_call.returns("stub")

    with plain, timeline() as tape:
        Gateway().charge(1)

    assert tape.all[0].phase is None
    assert tape.all[0].injected

    charge = binding(Gateway, "charge")
    charge.on_call.raises(TimeoutError("down"))
    charge.on_call.then(after=2).passes_through()

    with charge, timeline() as tape:
        gateway = Gateway()
        for _ in range(2):
            with pytest.raises(TimeoutError):
                gateway.charge(1)
        gateway.charge(1)

    assert [event.phase for event in tape.all] == [0, 0, 1]
    assert [event.injected for event in tape.all] == [True, True, False]


def test_phase_rides_into_json_records() -> None:
    from wrapture.sinks import _event_record

    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")
    charge.on_call.then(after=1).returns("b")

    with charge, timeline() as tape:
        Gateway().charge(1)
        Gateway().charge(1)

    records = [_event_record(event) for event in tape.all]
    assert [record["phase"] for record in records] == [0, 1]


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_exactly_n_calls_run_under_a_count_phase_with_concurrent_callers() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("first")
    charge.on_call.then(after=50).returns("second")

    gateway = Gateway()
    start = threading.Barrier(8)

    def worker() -> list[str]:
        start.wait()
        return [gateway.charge(1) for _ in range(25)]

    with charge, ThreadPoolExecutor(max_workers=8) as pool:
        results = [
            item for batch in pool.map(lambda _: worker(), range(8)) for item in batch
        ]

    assert results.count("first") == 50
    assert results.count("second") == 150


# ---------------------------------------------------------------------------
# returns_from(): sequences
# ---------------------------------------------------------------------------


def test_returns_from_yields_successive_values_lazily() -> None:
    drawn: list[int] = []

    def numbers() -> Any:
        for n in (10, 20, 30):
            drawn.append(n)
            yield n

    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from(numbers())

    with lookup, timeline() as tape:
        gateway = Gateway()
        assert drawn == []
        assert gateway.charge(1) == 10
        assert drawn == [10]
        assert gateway.charge(1) == 20
        assert gateway.charge(1) == 30

    assert all(event.injected for event in tape.all)


def test_exhaustion_hands_the_call_to_the_successor() -> None:
    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from([1, 2])

    settled = lookup.on_call.then()
    settled.returns("default")

    with lookup, timeline() as tape:
        gateway = Gateway()
        assert [gateway.charge(1) for _ in range(4)] == [1, 2, "default", "default"]

    assert lookup.phase == 1
    assert [event.phase for event in tape.all] == [0, 0, 1, 1]


def test_exhaustion_then_pass_through_and_the_event_is_restamped() -> None:
    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from(["a"])
    lookup.on_call.then()

    with lookup, timeline() as tape:
        gateway = Gateway()
        assert gateway.charge(1) == "a"
        assert gateway.charge(2) == {"id": "ch_2", "amount": 2}

    second = tape.all[1]
    assert second.phase == 1
    assert not second.injected


def test_exhaustion_with_no_successor_raises() -> None:
    from wrapture import SequenceExhaustedError

    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from(iter([1]))

    with lookup:
        gateway = Gateway()
        assert gateway.charge(1) == 1

        with pytest.raises(SequenceExhaustedError, match="phase 0 is exhausted"):
            gateway.charge(1)

        # and stays that way

        with pytest.raises(SequenceExhaustedError):
            gateway.charge(1)


def test_a_count_exit_still_applies_to_a_sequence_phase() -> None:
    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from([1, 2, 3, 4])
    lookup.on_call.then(after=2).returns("cut")

    with lookup:
        gateway = Gateway()
        assert [gateway.charge(1) for _ in range(3)] == [1, 2, "cut"]


def test_stages_wrap_each_value_from_the_sequence() -> None:
    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from([1, 2, 3])
    lookup.on_call.transforms_result(lambda n: n * 10)

    # Stages compose in the order added, so the transform is outermost
    # and the check sees the raw value.

    def refuse_two(n: int) -> None:
        if n == 2:
            raise ValueError("two")

    lookup.on_call.validates_result(refuse_two)

    with lookup:
        gateway = Gateway()
        assert gateway.charge(1) == 10
        with pytest.raises(ValueError, match="two"):
            gateway.charge(1)
        assert gateway.charge(1) == 30


def test_apply_restarts_a_list_sequence_but_a_generator_continues() -> None:
    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from([1, 2])
    lookup.on_call.then().returns("done")

    gateway = Gateway()

    with lookup:
        assert [gateway.charge(1) for _ in range(3)] == [1, 2, "done"]

    with lookup:
        assert [gateway.charge(1) for _ in range(3)] == [1, 2, "done"]

    generated = binding(Gateway, "charge")
    generated.on_call.returns_from(n for n in (1, 2, 3))
    generated.on_call.then().returns("done")

    with generated:
        assert gateway.charge(1) == 1

    with generated:
        assert [gateway.charge(1) for _ in range(3)] == [2, 3, "done"]


def test_a_new_terminal_or_passes_through_drops_the_sequence() -> None:
    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from([1, 2])
    lookup.on_call.returns("fixed")

    with lookup:
        gateway = Gateway()
        assert [gateway.charge(1) for _ in range(3)] == ["fixed"] * 3

    lookup.on_call.returns_from([1, 2])
    lookup.on_call.passes_through()

    with lookup:
        assert gateway.charge(1)["id"] == "ch_1"


def test_random_random_can_be_made_deterministic() -> None:
    import random

    with binding(random, "random").on_call.returns_from([0.1, 0.9, 0.5]):
        assert [random.random() for _ in range(3)] == [0.1, 0.9, 0.5]

    with binding(random.Random, "random").on_call.returns_from([0.25, 0.75]):
        own = random.Random()
        assert [own.random() for _ in range(2)] == [0.25, 0.75]

    assert 0.0 <= random.random() < 1.0


def test_attribute_reads_draw_from_a_sequence() -> None:
    retries = binding(Settings, "retries")
    retries.on_get.returns_from([1, 2])

    steady = retries.on_get.then()
    steady.returns(5)

    with retries:
        settings = Settings()
        assert [settings.retries for _ in range(4)] == [1, 2, 5, 5]

    assert retries.on_get.phase == 1


def test_concurrent_callers_each_draw_one_value() -> None:
    lookup = binding(Gateway, "charge")
    lookup.on_call.returns_from(range(200))
    lookup.on_call.then().returns("done")

    gateway = Gateway()
    start = threading.Barrier(8)

    def worker() -> list[Any]:
        start.wait()
        return [gateway.charge(1) for _ in range(30)]

    with lookup, ThreadPoolExecutor(max_workers=8) as pool:
        results = [
            item for batch in pool.map(lambda _: worker(), range(8)) for item in batch
        ]

    numbers = sorted(item for item in results if item != "done")
    assert numbers == list(range(200))
    assert results.count("done") == 40


# ---------------------------------------------------------------------------
# then(until=...): predicate exits
# ---------------------------------------------------------------------------


class Fetcher:
    def __init__(self) -> None:
        self.calls = 0

    # Returns Any so tests can compare stubbed values of other types.

    def fetch(self, page: int) -> Any:
        self.calls += 1
        if page == 3:
            raise ConnectionError("upstream")
        return {"page": page, "items": [page] * 2}

    async def afetch(self, page: int) -> Any:
        return self.fetch(page)

    def stream(self, page: int) -> Any:
        yield page


def test_circuit_breaker_trips_once_a_call_raised() -> None:
    fetch = binding(Fetcher, "fetch")
    fetch.on_call.passes_through()

    tripped = fetch.on_call.then(until=lambda e: e.exception is not None)
    tripped.raises(RuntimeError("circuit open"))

    fetcher = Fetcher()

    with fetch, timeline() as tape:
        assert fetcher.fetch(1)["page"] == 1
        with pytest.raises(ConnectionError):
            fetcher.fetch(3)
        with pytest.raises(RuntimeError, match="circuit open"):
            fetcher.fetch(1)
        with pytest.raises(RuntimeError):
            fetcher.fetch(2)

    assert fetch.phase == 1
    assert fetcher.calls == 2
    assert [event.phase for event in tape.all] == [0, 0, 1, 1]


def test_pagination_ends_once_the_last_page_was_asked_for() -> None:
    pages = binding(Fetcher, "fetch")

    exhausted = pages.on_call.then(until=lambda e: e.arguments["page"] >= 2)
    exhausted.returns({"page": None, "items": []})

    fetcher = Fetcher()

    with pages:
        assert fetcher.fetch(1)["items"] == [1, 1]
        assert fetcher.fetch(2)["items"] == [2, 2]  # the last real page
        assert fetcher.fetch(3)["items"] == []  # would have raised

    assert pages.phase == 1


def test_predicate_sees_the_result_as_the_caller_saw_it() -> None:
    fetch = binding(Fetcher, "fetch")
    fetch.on_call.transforms_result(lambda page: page["items"])

    seen: list[Any] = []

    def empty(event: Any) -> bool:
        seen.append(event.result)
        return bool(event.result == [])

    fetch.on_call.then(until=empty).returns("after")

    fetcher = Fetcher()

    with fetch:
        assert fetcher.fetch(1) == [1, 1]
        assert seen == [[1, 1]]


def test_predicate_sees_a_stage_failure_as_an_exception() -> None:
    fetch = binding(Fetcher, "fetch")

    def refuse(result: Any) -> None:
        raise ValueError("bad page")

    fetch.on_call.validates_result(refuse)
    fetch.on_call.then(until=lambda e: isinstance(e.exception, ValueError)).returns(
        "ok"
    )

    fetcher = Fetcher()

    with fetch:
        with pytest.raises(ValueError):
            fetcher.fetch(1)
        assert fetcher.fetch(1) == "ok"


def test_predicate_is_evaluated_without_a_timeline_and_when_filtered() -> None:
    fetch = binding(Fetcher, "fetch")
    fetch.on_call.then(until=lambda e: e.arguments["page"] == 2).returns("done")

    fetcher = Fetcher()

    with fetch:  # no timeline running
        fetcher.fetch(1)
        fetcher.fetch(2)
        assert fetcher.fetch(1) == "done"

    quiet = binding(Fetcher, "fetch", when=False)
    quiet.on_call.then(until=lambda e: e.exception is not None).returns("done")

    with quiet:
        with pytest.raises(ConnectionError):
            fetcher.fetch(3)
        assert fetcher.fetch(1) == "done"

    filtered = binding(Fetcher, "fetch", when=lambda i, a, k: False)
    filtered.on_call.then(until=lambda e: e.arguments["page"] == 1).returns("done")

    with filtered, timeline() as tape:
        fetcher.fetch(1)
        assert fetcher.fetch(1) == "done"

    assert len(tape.all) == 0


def test_predicate_gets_arguments_and_result_even_when_sinks_capture_nothing() -> None:
    from wrapture import Counter, window

    fetch = binding(Fetcher, "fetch")
    fetch.on_call.then(until=lambda e: e.arguments["page"] == 1 and e.result).returns(
        "done"
    )

    fetcher = Fetcher()

    with fetch, window(collect=[Counter()]):
        fetcher.fetch(1)
        assert fetcher.fetch(1) == "done"


def test_predicate_on_an_async_target_sees_the_awaited_outcome() -> None:
    import asyncio

    afetch = binding(Fetcher, "afetch")
    afetch.on_call.then(until=lambda e: e.result["page"] == 2).returns("done")

    fetcher = Fetcher()

    async def run() -> list[Any]:
        first = await fetcher.afetch(1)
        second = await fetcher.afetch(2)
        third = fetcher.afetch(3)
        return [first, second, third]

    with afetch:
        first, second, third = asyncio.run(run())

    assert first["page"] == 1
    assert second["page"] == 2
    assert third == "done"


def test_predicate_on_a_generator_target_sees_the_call_at_construction() -> None:
    stream = binding(Fetcher, "stream")
    stream.on_call.then(until=lambda e: e.arguments["page"] == 1).returns(iter([]))

    fetcher = Fetcher()

    with stream:
        assert list(fetcher.stream(1)) == [1]
        assert list(fetcher.stream(2)) == []


def test_predicate_exit_on_an_attribute_read() -> None:
    retries = binding(Settings, "retries")
    retries.on_get.returns(1)

    steady = retries.on_get.then(until=lambda e: e.result == 1)
    steady.returns(9)

    with retries:
        settings = Settings()
        assert settings.retries == 1
        assert settings.retries == 9

    with retries, timeline():
        assert settings.retries == 1
        assert settings.retries == 9


def test_predicate_exit_on_an_attribute_write_sees_the_value() -> None:
    limit = binding(Settings, "retries")

    frozen = limit.on_set.then(until=lambda e: e.value >= 5)
    frozen.rejects()

    with limit:
        settings = Settings()
        settings.retries = 2
        settings.retries = 5
        with pytest.raises(AttributeError):
            settings.retries = 6

    assert limit.on_set.phase == 1


def test_a_raising_predicate_reaches_the_caller() -> None:
    fetch = binding(Fetcher, "fetch")

    def broken(event: Any) -> bool:
        raise KeyError("oops")

    fetch.on_call.then(until=broken).returns("done")

    with fetch:
        with pytest.raises(KeyError, match="oops"):
            Fetcher().fetch(1)


# ---------------------------------------------------------------------------
# reset(), in_phase() and groups
# ---------------------------------------------------------------------------


def test_passes_through_on_the_base_clears_only_phase_zero() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")
    charge.on_call.then(after=1).returns("b")

    charge.on_call.passes_through()

    with charge:
        gateway = Gateway()
        assert gateway.charge(1)["id"] == "ch_1"
        assert gateway.charge(1) == "b"


def test_reset_drops_the_whole_chain() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")
    charge.on_call.then(after=1).returns("b")

    assert charge.on_call.reset() is charge

    with charge:
        gateway = Gateway()
        assert gateway.charge(1)["id"] == "ch_1"
        assert gateway.charge(1)["id"] == "ch_1"

    assert charge.phase == 0
    assert not hasattr(charge.on_call.then(), "reset")


def test_reset_on_an_attribute_operation() -> None:
    flag = binding(Settings, "beta_enabled")
    flag.on_get.returns(True)
    flag.on_get.then(after=1).returns(False)
    flag.on_get.reset()

    with flag:
        assert Settings().beta_enabled is False

    assert flag.phase == 0


def test_in_phase_filters_events_by_handling_phase() -> None:
    from wrapture import ExpectationNotMetError

    charge = binding(Gateway, "charge")
    charge.on_call.raises(TimeoutError("down"))
    charge.on_call.then(after=2).passes_through()

    with charge, timeline() as tape:
        gateway = Gateway()
        for _ in range(2):
            with pytest.raises(TimeoutError):
                gateway.charge(1)
        gateway.charge(1)

    tape.for_binding(charge).in_phase(0).assert_times(2)
    tape.for_binding(charge).in_phase(1).assert_once()
    tape.for_binding(charge).in_phase(2).assert_never()

    with pytest.raises((AssertionError, ExpectationNotMetError), match=r"in_phase=1"):
        tape.for_binding(charge).in_phase(1).assert_times(2)


def test_in_phase_never_matches_events_of_an_unphased_binding() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.returns("a")

    with charge, timeline() as tape:
        Gateway().charge(1)

    tape.for_binding(charge).in_phase(0).assert_never()


def test_a_group_advances_every_member() -> None:
    from wrapture import bindings

    group = bindings(
        charge=(Gateway, "charge"),
        flag=(Settings, "beta_enabled"),
    )
    group.charge.on_call.returns("down")
    group.charge.on_call.then().returns("up")
    group.flag.on_get.returns(False)
    group.flag.on_get.then().returns(True)

    with group:
        assert Gateway().charge(1) == "down"
        assert Settings().beta_enabled is False

        assert group.advance() is group

        assert Gateway().charge(1) == "up"
        assert Settings().beta_enabled is True
