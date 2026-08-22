"""Tests for process-fork behaviour.

These cover the at-fork handlers the first sink registration
installs: the child discarding the inherited in-flight stack and
active trace, recording working afresh in the child (locks and
reentrancy state reinitialised), the Sink.on_fork() notification
being delivered only in the child, and JSONLines giving a forked
child its own file through a {pid} template.

The tests fork for real with os.fork(), so they are skipped where
fork does not exist (Windows). Each child communicates through its
exit code and exits with os._exit, so no test machinery runs twice.
"""

import json
import os
from pathlib import Path

import pytest

import wrapture

pytestmark = [
    pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available"),
    # Forking a process that has threads (writer threads from other
    # tests' sinks, say) raises DeprecationWarning on 3.12+; these
    # tests fork deliberately and briefly.
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


class _Collector(wrapture.Sink):
    capture_args = "none"
    capture_result = "none"

    def __init__(self) -> None:
        self.entered: list[str] = []
        self.forked = 0

    def on_enter(self, event: wrapture.Event) -> None:
        self.entered.append(str(event))

    def on_fork(self) -> None:
        self.forked += 1


def _wait_ok(pid: int) -> None:
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_a_child_discards_the_inherited_stack_and_trace() -> None:
    # The in-flight events belong to the parent, which will run their
    # bodies and close them: immediately after the fork the child is
    # in the nothing-in-flight state, and its first operation is a
    # genuine root that mints its own trace.

    sink = _Collector()
    wrapture.add_sink(sink)
    try:
        with wrapture.block("parent-work"):
            parent_trace = wrapture.current_trace()
            pid = os.fork()

            if pid == 0:
                try:
                    ok = (
                        wrapture.current_event() is None
                        and wrapture.current_trace() is None
                    )

                    with wrapture.block("child-work"):
                        child_trace = wrapture.current_trace()
                        ok = (
                            ok
                            and child_trace is not None
                            and child_trace is not parent_trace
                        )
                except BaseException:
                    os._exit(1)
                os._exit(0 if ok else 1)

            _wait_ok(pid)
    finally:
        wrapture.remove_sink(sink)


def test_on_fork_is_delivered_only_in_the_child() -> None:
    sink = _Collector()
    wrapture.add_sink(sink)
    try:
        pid = os.fork()

        if pid == 0:
            os._exit(0 if sink.forked == 1 else 1)

        _wait_ok(pid)
        assert sink.forked == 0
    finally:
        wrapture.remove_sink(sink)


def test_recording_works_afresh_in_the_child() -> None:
    # The child records through the same registered sinks with
    # reinitialised locks: a whole tree opens and closes cleanly.

    sink = _Collector()
    wrapture.add_sink(sink)
    try:
        pid = os.fork()

        if pid == 0:
            try:
                before = len(sink.entered)
                with wrapture.block("outer"):
                    with wrapture.block("inner"):
                        pass
                ok = len(sink.entered) == before + 2
            except BaseException:
                os._exit(1)
            os._exit(0 if ok else 1)

        _wait_ok(pid)
    finally:
        wrapture.remove_sink(sink)


def test_jsonlines_gives_the_child_its_own_file(tmp_path: Path) -> None:
    # The {pid} template is exactly the fork anticipation: the parent
    # flushes before the fork, the child's on_fork drops the dead
    # writer and expands the template afresh, and each process ends up
    # writing whole lines to a file of its own.

    sink = wrapture.JSONLines(tmp_path / "trace-{pid}.jsonl")
    wrapture.add_sink(sink)
    try:
        with wrapture.block("parent-side"):
            pass
        wrapture.flush_sinks()

        pid = os.fork()

        if pid == 0:
            try:
                with wrapture.block("child-side"):
                    pass
                wrapture.flush_sinks()

                path = sink.path
                ok = path is not None and str(os.getpid()) in path
            except BaseException:
                os._exit(1)
            os._exit(0 if ok else 1)

        _wait_ok(pid)
    finally:
        wrapture.remove_sink(sink)
        sink.close()

    parent_file = tmp_path / f"trace-{os.getpid()}.jsonl"
    child_file = tmp_path / f"trace-{pid}.jsonl"

    assert parent_file.exists() and child_file.exists()

    (parent_line,) = parent_file.read_text().splitlines()
    (child_line,) = child_file.read_text().splitlines()
    assert json.loads(parent_line)["label"] == "parent-side"
    assert json.loads(child_line)["label"] == "child-side"


def test_the_handlers_install_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Registration is once per process however many sinks come and
    # go; the hook itself is idempotent through the _fork_hooked flag.

    from wrapture import lifecycle

    calls: list[int] = []
    monkeypatch.setattr(lifecycle, "_fork_hooked", False)
    monkeypatch.setattr(os, "register_at_fork", lambda **kwargs: calls.append(1))

    first = wrapture.add_sink(_Collector())
    second = wrapture.add_sink(_Collector())
    try:
        assert calls == [1]
    finally:
        wrapture.remove_sink(first)
        wrapture.remove_sink(second)
