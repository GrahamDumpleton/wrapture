"""Process lifecycle: shutdown and fork.

Shutdown is the one call that makes everything owing output deliver
it. Subsystems that hold something at process exit (a window with an
open run, sinks with buffered lines) register a shutdown step here as
soon as they have something to tear down. shutdown() runs the steps
in phase order, each isolated from the others' failures, and is
idempotent: every step is repeatable, so a host may call it more than
once. The first registration installs an atexit hook that calls
shutdown(), so ordinary interpreter exit does the same thing.

Shutdown quiesces; it does not uninstall. Bindings stay applied and
configs are not reverted, so a host that calls it early has flushed
its output but not torn out its tracing.

The fork handlers live here too, installed automatically when the
first sink is registered. Before a fork the sinks are flushed, so
buffered output is not duplicated into the child, and the recording
locks are taken, so the child does not inherit one mid-acquire; the
parent releases them afterwards. The child reinitialises instead:
fresh locks, the in-flight stack cleared (those events belong to the
parent, which will run their bodies and close them), and every
process sink notified through Sink.on_fork() so it can rebuild what
fork broke. Spawned or exec'd children are a non-issue: they start
fresh and deliberately untraced.
"""

from __future__ import annotations

import atexit
import os
import threading
import warnings
from collections.abc import Callable
from typing import Any

__all__ = ["shutdown"]


# Phases, lowest first: windows close (delivering their reports and
# flushing their per-run sinks) before the always-on sinks flush.

WINDOWS = 10
SINKS = 20

_steps: list[tuple[int, int, str, Callable[[], None]]] = []
_lock = threading.Lock()
_hooked = False
_counter = 0


def _on_shutdown(name: str, callback: Callable[[], None], *, phase: int) -> None:
    # Register a shutdown step. Registering the same callback again is
    # a no-op, so a subsystem can register whenever it first has work
    # without tracking whether it already did.

    global _hooked, _counter

    with _lock:
        if any(existing is callback for _, _, _, existing in _steps):
            return

        _counter += 1
        _steps.append((phase, _counter, name, callback))

        if not _hooked:
            _hooked = True
            atexit.register(shutdown)


_fork_hooked = False


def _register_at_fork() -> None:
    # Install the process-wide fork handlers once. Called when the
    # first sink is registered; benign if the process never forks.

    global _fork_hooked

    with _lock:
        if _fork_hooked:
            return
        _fork_hooked = True

    if hasattr(os, "register_at_fork"):
        os.register_at_fork(
            before=_before_fork,
            after_in_parent=_after_fork_in_parent,
            after_in_child=_after_fork_in_child,
        )


def _recording_modules() -> tuple[Any, Any]:
    # The sinks and timeline modules, fetched by import_module because
    # the package rebinds the name "timeline" to the context manager,
    # so a `from . import timeline` would hand back the function.

    from importlib import import_module

    return import_module("wrapture.sinks"), import_module("wrapture.timeline")


def _before_fork() -> None:
    # Flush buffered output, so it is not duplicated into the child,
    # then take the recording locks, so the child does not inherit one
    # held by a thread that will not exist there.

    sinks, timeline = _recording_modules()

    sinks._flush_process_sinks()

    sinks._registry_lock.acquire()
    sinks._seq_lock.acquire()
    timeline._active_lock.acquire()
    timeline._block_recorders_lock.acquire()


def _after_fork_in_parent() -> None:
    sinks, timeline = _recording_modules()

    timeline._block_recorders_lock.release()
    timeline._active_lock.release()
    sinks._seq_lock.release()
    sinks._registry_lock.release()


def _after_fork_in_child() -> None:
    # The child reinitialises rather than releases: fresh locks, the
    # in-flight stack and reentrancy guard cleared (the in-flight
    # events belong to the parent, which will run their bodies and
    # close them; a child that kept the stack would nest its first
    # event under an operation completing in another process), then
    # every process sink notified so it can rebuild what fork broke.

    sinks, timeline = _recording_modules()

    sinks._registry_lock = threading.Lock()
    sinks._seq_lock = threading.Lock()
    timeline._active_lock = threading.Lock()
    timeline._block_recorders_lock = threading.Lock()

    timeline._stack.set(())
    sinks._in_recorder.set(False)

    for sink in tuple(sinks._process_sinks):
        try:
            sink.on_fork()
        except Exception:
            sinks._note_sink_error(sink)


def shutdown() -> None:
    """Deliver everything owed at process exit: close open window runs
    (writing their reports), then flush every process sink.

    The same operation the atexit hook performs, callable directly for
    environments where atexit cannot be relied on: an embedded or sub
    interpreter may be destroyed without its atexit callbacks ever
    running. Hosts such as mod_wsgi provide their own process shutdown
    notification that fires while the interpreter is still fully
    alive; subscribe that notification to this function. Safe to call
    any number of times, and nothing is uninstalled: bindings stay
    applied and configs are not reverted. A step that raises is warned
    about and the remaining steps still run.
    """

    with _lock:
        steps = sorted(_steps, key=lambda step: (step[0], step[1]))

    for _, _, name, callback in steps:
        try:
            callback()
        except Exception as exc:
            warnings.warn(
                f"shutdown step {name!r} raised {exc!r}; the remaining steps still run",
                RuntimeWarning,
                stacklevel=2,
            )
