"""Tests for the thread recording gap: detection, counting, and the
context-propagation workaround.

A thread without the caller's context sees no ambient tape and records
nothing. That is accepted; what must not happen is the tape silently
looking complete. Calls arriving with no context while a timeline is
active elsewhere are counted on the binding and warned about, and a
thread handed a copied context records normally.

Whether a plain Thread has the caller's context depends on the build:
Python 3.14 added Thread(context=...) with inheritance of a copy by
default where sys.flags.thread_inherit_context is set, which is the
free-threaded default. The gap tests therefore force a fresh empty
context on 3.14+, making the no-context case deterministic everywhere;
a separate test pins the inheriting behaviour where the build has it.
"""

import asyncio
import contextvars
import sys
import threading
import warnings
from typing import Any

import pytest

from wrapture import RecordingGapWarning, binding, propagate, timeline

_THREADS_TAKE_CONTEXT = sys.version_info >= (3, 14)
_THREADS_INHERIT = bool(getattr(sys.flags, "thread_inherit_context", 0))


class Ledger:
    def record(self, entry: str) -> str:
        return f"recorded:{entry}"


class Model:
    status = "draft"


def _call_in_thread(fn: Any, *args: Any) -> list[warnings.WarningMessage]:
    """Run fn(*args) on a thread with no recording context, returning
    warnings raised there.

    On 3.14+ the thread is given a fresh empty context explicitly, so
    the no-context case is exercised even on builds where threads
    inherit by default. Warnings are captured inside the worker, where
    the warn call happens, so the capture works on builds where warning
    state is context-local.
    """

    caught: list[warnings.WarningMessage] = []

    def worker() -> None:
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            fn(*args)
        caught.extend(seen)

    extra: dict[str, Any] = {}
    if _THREADS_TAKE_CONTEXT:
        extra["context"] = contextvars.Context()

    thread = threading.Thread(target=worker, **extra)
    thread.start()
    thread.join()

    return caught


# ---------------------------------------------------------------------------
# the gap is detected and counted
# ---------------------------------------------------------------------------


def test_a_thread_call_during_a_timeline_warns_and_counts() -> None:
    record = binding(Ledger, "record")

    with timeline(record):
        caught = _call_in_thread(Ledger().record, "t1")

        record.events.assert_never()
        assert record.missed_calls == 1

    (warning,) = caught
    assert issubclass(warning.category, RecordingGapWarning)
    assert "Ledger.record" in str(warning.message)
    assert "propagate" in str(warning.message)


def test_every_miss_is_counted_but_only_the_first_warns() -> None:
    record = binding(Ledger, "record")

    def hammer() -> None:
        ledger = Ledger()
        for n in range(5):
            ledger.record(f"t{n}")

    with timeline(record):
        caught = _call_in_thread(hammer)

        assert record.missed_calls == 5

    assert len(caught) == 1


def test_behaviour_still_applies_on_the_thread() -> None:
    record = binding(Ledger, "record").on_call.returns("stubbed")

    results: list[str] = []

    with timeline(record):
        _call_in_thread(lambda: results.append(Ledger().record("t1")))

    assert results == ["stubbed"]


def test_attribute_access_on_a_thread_is_detected_too() -> None:
    status = binding(Model, "status", missing_ok=True)
    model = Model()

    with timeline(status):
        caught = _call_in_thread(lambda: model.status)

        status.events.assert_never()
        assert status.missed_calls == 1

    (warning,) = caught
    assert issubclass(warning.category, RecordingGapWarning)


# ---------------------------------------------------------------------------
# no false alarms
# ---------------------------------------------------------------------------


def test_no_warning_when_no_timeline_is_active_anywhere() -> None:
    record = binding(Ledger, "record")

    with record:
        caught = _call_in_thread(Ledger().record, "t1")

    assert caught == []
    assert record.missed_calls == 0


def test_a_fresh_apply_may_warn_again() -> None:
    record = binding(Ledger, "record")

    with timeline(record):
        first = _call_in_thread(Ledger().record, "t1")

    with timeline(record):
        second = _call_in_thread(Ledger().record, "t2")

    assert len(first) == 1
    assert len(second) == 1
    assert record.missed_calls == 2


@pytest.mark.skipif(
    not _THREADS_INHERIT,
    reason="threads do not inherit context on this build",
)
def test_a_plain_thread_records_where_the_build_inherits_context() -> None:
    # Where sys.flags.thread_inherit_context is set (the free-threaded
    # default from 3.14), a plain Thread carries a copy of the caller's
    # context, so there is no gap: the call records normally.

    record = binding(Ledger, "record")

    with timeline(record):
        thread = threading.Thread(target=Ledger().record, args=("t1",))
        thread.start()
        thread.join()

        record.events.assert_once()
        assert record.missed_calls == 0


# ---------------------------------------------------------------------------
# the asyncio thread bridges split along the context line
# ---------------------------------------------------------------------------


def test_asyncio_to_thread_records_because_it_copies_context() -> None:
    record = binding(Ledger, "record")

    with timeline(record):
        asyncio.run(asyncio.to_thread(Ledger().record, "bridged"))

        record.events.assert_once()
        assert record.missed_calls == 0


def test_run_in_executor_hits_the_thread_gap() -> None:
    from concurrent.futures import ThreadPoolExecutor

    record = binding(Ledger, "record")

    # run_in_executor never propagates context per call: a pool worker
    # keeps whatever context existed when that worker thread was first
    # created (on builds where threads inherit at creation), and has
    # none at all elsewhere. Pre-spawning the worker outside any
    # timeline pins the realistic pool-warmed-at-startup case on every
    # build.

    executor = ThreadPoolExecutor(max_workers=1)
    executor.submit(lambda: None).result()

    async def offload() -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, Ledger().record, "offloaded")

    try:
        with timeline(record):
            # Absorb the once-per-apply warning on a plain thread first,
            # so the executor worker's miss is counted without emitting
            # a warning this test would have to intercept in the pool.

            _call_in_thread(Ledger().record, "pre")

            asyncio.run(offload())

            record.events.assert_never()
            assert record.missed_calls == 2
    finally:
        executor.shutdown()


# ---------------------------------------------------------------------------
# the workaround: hand the thread a copied context
# ---------------------------------------------------------------------------


def test_a_thread_with_a_copied_context_records_onto_the_tape() -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape:
        # The copy must be taken inside the timeline, so the ambient
        # tape is part of what the thread inherits.

        context = contextvars.copy_context()
        thread = threading.Thread(
            target=context.run, args=(Ledger().record, "threaded")
        )
        thread.start()
        thread.join()

        Ledger().record("direct")

        record.events.assert_times(2)
        assert record.missed_calls == 0

    threaded, direct = tape.all
    assert threaded.args == ("threaded",)
    assert threaded.depth == 0
    assert direct.args == ("direct",)


def test_propagate_carries_the_timeline_into_a_thread() -> None:
    record = binding(Ledger, "record")

    with timeline(record) as tape:
        # propagate() wraps the manual copy above: called inside the
        # timeline, so the recording context is part of what it takes.

        work = propagate(Ledger().record)
        thread = threading.Thread(target=work, args=("threaded",))
        thread.start()
        thread.join()

        record.events.assert_once()
        assert record.missed_calls == 0

    assert tape.all[0].args == ("threaded",)


def test_one_propagated_callable_is_shared_by_several_threads() -> None:
    record = binding(Ledger, "record")

    with timeline(record):
        # Each invocation runs in its own copy of the captured context,
        # so concurrent threads never contend for one Context object.

        work = propagate(Ledger().record)
        threads = [threading.Thread(target=work, args=(f"t{n}",)) for n in range(4)]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        record.events.assert_times(4)
        assert record.missed_calls == 0


def test_a_propagated_thread_outliving_the_timeline_discards_visibly() -> None:
    record = binding(Ledger, "record")
    release = threading.Event()

    def late() -> None:
        release.wait(timeout=5)
        Ledger().record("late")

    # The binding outlives the timeline, so the late call is still
    # observed; only the recording scope has ended by then.

    with record:
        with timeline() as tape:
            Ledger().record("in time")
            thread = threading.Thread(target=propagate(late))
            thread.start()

        release.set()
        thread.join()

    # The late event is dropped and counted, never appended: the tape
    # a test asserted on cannot change shape afterwards.

    assert [event.args for event in tape.all] == [("in time",)]
    assert tape.discarded == 1
    assert repr(tape) == "<Tape: 1 event, 1 discarded after close>"
