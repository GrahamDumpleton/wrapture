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


__version_info__ = ("1", "0", "0", "dev1")
__version__ = _format_version(__version_info__)

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
    NONE,
    REFERENCE,
    SNAPSHOT,
    SUMMARY,
    TYPES,
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
    AbandonBehaviour,
    ErrorBehaviour,
    FinishBehaviour,
    ItemBehaviour,
    IteratorProxy,
    iterator,
)
from .timeline import (
    Tape,
    Timeline,
    annotate,
    current_event,
    timeline,
)

__all__ = [
    "NONE",
    "REFERENCE",
    "SNAPSHOT",
    "SUMMARY",
    "TYPES",
    "AbandonBehaviour",
    "AlreadyAppliedError",
    "Binding",
    "BindingGroup",
    "CallBehaviour",
    "DeferredTargetError",
    "DeleteBehaviour",
    "ErrorBehaviour",
    "Event",
    "EventLog",
    "ExpectationNotMetError",
    "FinishBehaviour",
    "GetBehaviour",
    "ItemBehaviour",
    "IteratorProxy",
    "NeverAppliedError",
    "NotImplementedYetError",
    "RecordingGapWarning",
    "SetBehaviour",
    "Tape",
    "Timeline",
    "WrongModeError",
    "annotate",
    "binding",
    "bindings",
    "current_event",
    "iterator",
    "redact",
    "timeline",
]
