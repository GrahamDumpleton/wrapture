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
from .exceptions import (
    AlreadyAppliedError,
    DeferredTargetError,
    NotImplementedYetError,
    WrongModeError,
)

__all__ = [
    "AlreadyAppliedError",
    "Binding",
    "BindingGroup",
    "CallBehaviour",
    "DeferredTargetError",
    "DeleteBehaviour",
    "GetBehaviour",
    "NotImplementedYetError",
    "SetBehaviour",
    "WrongModeError",
    "binding",
    "bindings",
]
