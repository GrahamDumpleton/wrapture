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


__version_info__ = ("1", "0", "0", "a11")
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
    CaughtException,
    Event,
    EventLink,
)
from .exceptions import (
    AlreadyAppliedError,
    AmbiguousBindingError,
    ConfigError,
    ConfigWarning,
    DeferredTargetError,
    ExpectationNotMetError,
    NeverAppliedError,
    NoBindingError,
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
from .filters import (
    RequestFilter,
    filter_requests,
)
from .instrumentations import (
    Instrumentation,
    Instrumented,
    InstrumentEntry,
    Setting,
    instrumentation,
    instrumentation_hook,
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
from .logs import (
    LogCapture,
    capture_logs,
)
from .lookup import (
    Observer,
    binding_of,
    bindings_of,
    find_binding,
    find_bindings,
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
    Block,
    EventHandle,
    Handoff,
    Subtree,
    Tape,
    Timeline,
    annotate,
    block,
    current_event,
    current_trace,
    detach,
    handoff,
    note_exception,
    propagate,
    timeline,
    trace_headers,
)
from .trace import (
    TraceContext,
    TraceSlot,
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
    "AmbiguousBindingError",
    "AppliedConfig",
    "Binding",
    "BoundSpec",
    "Collector",
    "Report",
    "Run",
    "Window",
    "BindingGroup",
    "Block",
    "CallBehaviour",
    "CallPhase",
    "CaughtException",
    "Config",
    "ConfigError",
    "ConfigWarning",
    "Counter",
    "DeferredTargetError",
    "DeleteBehaviour",
    "DeletePhase",
    "Depth",
    "Event",
    "EventHandle",
    "EventLink",
    "EventLog",
    "ExpectationNotMetError",
    "Fanout",
    "Filter",
    "GetBehaviour",
    "GetPhase",
    "Handoff",
    "InstrumentEntry",
    "Instrumentation",
    "Instrumented",
    "IteratorAbandonBehaviour",
    "IteratorErrorBehaviour",
    "IteratorFinishBehaviour",
    "IteratorItemBehaviour",
    "IteratorProxy",
    "JSONLines",
    "LogCapture",
    "NeverAppliedError",
    "NoBindingError",
    "NotImplementedYetError",
    "ObserveEntry",
    "ObservedCallable",
    "Observer",
    "PathStats",
    "Printer",
    "RecordingGapWarning",
    "RequestFilter",
    "Sample",
    "SequenceExhaustedError",
    "SetBehaviour",
    "SetPhase",
    "Setting",
    "Sink",
    "SinkErrorWarning",
    "StackFrame",
    "StubCallable",
    "Subtree",
    "Tape",
    "Timeline",
    "TraceContext",
    "TraceSlot",
    "WSGIMiddleware",
    "WrongModeError",
    "add_sink",
    "annotate",
    "binding",
    "bindings",
    "binding_of",
    "bindings_of",
    "block",
    "bound",
    "canonical",
    "capture_logs",
    "chrome_trace",
    "clear_stacks",
    "current_event",
    "current_trace",
    "detach",
    "discover",
    "filter_requests",
    "find_binding",
    "find_bindings",
    "find_config",
    "flush_sinks",
    "handoff",
    "instrumentation",
    "instrumentation_hook",
    "iterator",
    "load_config",
    "load_events",
    "mermaid",
    "mock",
    "note_exception",
    "observed",
    "propagate",
    "redact",
    "remove_sink",
    "shutdown",
    "stack_frames",
    "stub",
    "taped",
    "timeline",
    "trace_headers",
    "window",
]
