"""
Wrapture is a library for attaching bindings to arbitrary Python call sites,
without modifying the code being observed, for use in monkey patching,
testing and tracing.
"""


def _format_version(parts: tuple[str, ...]) -> str:
    base = ".".join(parts[:3])

    if len(parts) == 3:
        return base

    suffix = parts[3]
    return (
        f"{base}.{suffix}" if suffix.startswith(("dev", "post")) else f"{base}{suffix}"
    )


__version_info__ = ("1", "0", "0", "a1")
__version__ = _format_version(__version_info__)

from wrapt import MISSING, register_post_import_hook, when_imported

from .asgi import (
    ASGIMiddleware,
)
from .behaviours import (
    CallBehaviour,
    CallPhase,
    DeleteBehaviour,
    DeletePhase,
    GetBehaviour,
    GetPhase,
    SetBehaviour,
    SetPhase,
)
from .bindings import (
    Binding,
    BindingGroup,
    binding,
    bindings,
    discover,
)
from .capture import (
    redact,
)
from .collectors import (
    Aggregate,
    Counter,
    PathStats,
)
from .config import (
    AppliedConfig,
    Config,
    ObserveEntry,
    SetupEntry,
    find_config,
    load_config,
)
from .decorators import (
    BoundSpec,
    bound,
    taped,
)
from .doubles import (
    StubCallable,
    mock,
    stub,
)
from .eventlogs import (
    EventLog,
)
from .events import (
    Event,
)
from .exceptions import (
    AlreadyAppliedError,
    ConfigError,
    ConfigWarning,
    DeferredTargetError,
    ExpectationNotMetError,
    NeverAppliedError,
    NotImplementedYetError,
    RecordingGapWarning,
    SequenceExhaustedError,
    SinkErrorWarning,
    WrongModeError,
)
from .export import (
    canonical,
    chrome_trace,
    load_events,
    mermaid,
)
from .iterators import (
    IteratorAbandonBehaviour,
    IteratorErrorBehaviour,
    IteratorFinishBehaviour,
    IteratorItemBehaviour,
    IteratorProxy,
    iterator,
)
from .lifecycle import (
    shutdown,
)
from .observed import (
    ObservedCallable,
    observed,
)
from .sinks import (
    Depth,
    Fanout,
    Filter,
    JSONLines,
    Printer,
    Sample,
    Sink,
    add_sink,
    flush_sinks,
    remove_sink,
)
from .stacks import (
    StackFrame,
    clear_stacks,
    stack_frames,
)
from .timeline import (
    Tape,
    Timeline,
    annotate,
    current_event,
    propagate,
    timeline,
)
from .windows import (
    Collector,
    Report,
    Run,
    Window,
    window,
)
from .wsgi import (
    WSGIMiddleware,
)

__all__ = [
    "MISSING",
    "register_post_import_hook",
    "when_imported",
    "ASGIMiddleware",
    "Aggregate",
    "AlreadyAppliedError",
    "AppliedConfig",
    "Binding",
    "BoundSpec",
    "Collector",
    "Report",
    "Run",
    "Window",
    "BindingGroup",
    "CallBehaviour",
    "CallPhase",
    "Config",
    "ConfigError",
    "ConfigWarning",
    "Counter",
    "DeferredTargetError",
    "DeleteBehaviour",
    "DeletePhase",
    "Depth",
    "Event",
    "EventLog",
    "ExpectationNotMetError",
    "Fanout",
    "Filter",
    "GetBehaviour",
    "GetPhase",
    "IteratorAbandonBehaviour",
    "IteratorErrorBehaviour",
    "IteratorFinishBehaviour",
    "IteratorItemBehaviour",
    "IteratorProxy",
    "JSONLines",
    "NeverAppliedError",
    "NotImplementedYetError",
    "ObserveEntry",
    "ObservedCallable",
    "PathStats",
    "Printer",
    "RecordingGapWarning",
    "Sample",
    "SequenceExhaustedError",
    "SetBehaviour",
    "SetPhase",
    "SetupEntry",
    "Sink",
    "SinkErrorWarning",
    "StackFrame",
    "StubCallable",
    "Tape",
    "Timeline",
    "WSGIMiddleware",
    "WrongModeError",
    "add_sink",
    "annotate",
    "binding",
    "bindings",
    "bound",
    "canonical",
    "chrome_trace",
    "clear_stacks",
    "current_event",
    "discover",
    "find_config",
    "flush_sinks",
    "iterator",
    "load_config",
    "load_events",
    "mermaid",
    "mock",
    "observed",
    "propagate",
    "redact",
    "remove_sink",
    "shutdown",
    "stack_frames",
    "stub",
    "taped",
    "timeline",
    "window",
]
