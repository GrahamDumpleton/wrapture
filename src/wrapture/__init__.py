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
    "RecordingGapWarning",
    "SetBehaviour",
    "StackFrame",
    "Tape",
    "Timeline",
    "WrongModeError",
    "annotate",
    "binding",
    "bindings",
    "current_event",
    "iterator",
    "redact",
    "stack_frames",
    "timeline",
]
