"""
Wrapture is a library for attaching bindings to arbitrary Python call sites,
without modifying the code being observed, for use in monkey patching,
testing, tracing and profiling.
"""


def _format_version(parts: tuple[str, ...]) -> str:
    base = ".".join(parts[:3])

    if len(parts) == 3:
        return base

    suffix = parts[3]
    return (
        f"{base}.{suffix}" if suffix.startswith(("dev", "post")) else f"{base}{suffix}"
    )


__version_info__ = ("1", "0", "0", "dev2")
__version__ = _format_version(__version_info__)

from wrapt import MISSING

from .behaviours import (
    CallBehaviour,
    DeleteBehaviour,
    GetBehaviour,
    SetBehaviour,
)
from .bindings import (
    Binding,
    BindingGroup,
    binding,
    bindings,
)
from .capture import (
    redact,
)
from .eventlogs import (
    EventLog,
)
from .events import (
    Event,
)
from .exceptions import (
    AlreadyAppliedError,
    DeferredTargetError,
    ExpectationNotMetError,
    NeverAppliedError,
    NotImplementedYetError,
    RecordingGapWarning,
    SinkErrorWarning,
    WrongModeError,
)
from .iterators import (
    IteratorAbandonBehaviour,
    IteratorErrorBehaviour,
    IteratorFinishBehaviour,
    IteratorItemBehaviour,
    IteratorProxy,
    iterator,
)
from .sinks import (
    Printer,
    Sink,
    add_sink,
    flush_sinks,
    remove_sink,
)
from .stacks import (
    StackFrame,
    stack_frames,
)
from .timeline import (
    Tape,
    Timeline,
    annotate,
    current_event,
    timeline,
)

__all__ = [
    "MISSING",
    "AlreadyAppliedError",
    "Binding",
    "BindingGroup",
    "CallBehaviour",
    "DeferredTargetError",
    "DeleteBehaviour",
    "Event",
    "EventLog",
    "ExpectationNotMetError",
    "GetBehaviour",
    "IteratorAbandonBehaviour",
    "IteratorErrorBehaviour",
    "IteratorFinishBehaviour",
    "IteratorItemBehaviour",
    "IteratorProxy",
    "NeverAppliedError",
    "NotImplementedYetError",
    "Printer",
    "RecordingGapWarning",
    "SetBehaviour",
    "Sink",
    "SinkErrorWarning",
    "StackFrame",
    "Tape",
    "Timeline",
    "WrongModeError",
    "add_sink",
    "annotate",
    "binding",
    "bindings",
    "current_event",
    "flush_sinks",
    "iterator",
    "redact",
    "remove_sink",
    "stack_frames",
    "timeline",
]
