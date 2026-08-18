"""Process shutdown: the one call that makes everything owing output
deliver it.

Subsystems that hold something at process exit (a window with an open
run, sinks with buffered lines) register a shutdown step here as soon
as they have something to tear down. shutdown() runs the steps in
phase order, each isolated from the others' failures, and is
idempotent: every step is repeatable, so a host may call it more than
once. The first registration installs an atexit hook that calls
shutdown(), so ordinary interpreter exit does the same thing.

Shutdown quiesces; it does not uninstall. Bindings stay applied and
configs are not reverted, so a host that calls it early has flushed
its output but not torn out its tracing.
"""

from __future__ import annotations

import atexit
import threading
import warnings
from collections.abc import Callable

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
