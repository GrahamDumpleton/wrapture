"""Shared vocabulary for the OpenTelemetry sink modules."""

from __future__ import annotations

import traceback
from typing import Any

# Attribute values OTel accepts natively. Anything else is stringified.

_PRIMITIVES = (bool, int, float, str)

# The namespace wrapture-specific attribute names live under, for
# spans and metrics alike.

_PREFIX = "wrapture"

# Request data fields exported under their semantic-convention names,
# so the close-time sweep does not repeat them as wrapture.data.*. This
# is the reserved set the OTel docs page documents as the request
# data-key contract: annotating one of these on a request event opts
# into the same treatment.

_SEMCONV_DATA = frozenset({"method", "path", "query", "route"})


def _exception_attributes(
    exception: BaseException, level: str, *, escaped: bool = False
) -> dict[str, Any]:
    # The semconv exception attributes, holding as much of the
    # exception as the `exceptions=` level allows: the type name
    # always, the message at "message" and above, and at "full" the
    # stacktrace and whether the exception escaped the operation.

    attributes: dict[str, Any] = {"exception.type": type(exception).__name__}

    if level != "type":
        attributes["exception.message"] = str(exception)

    if level == "full":
        attributes["exception.stacktrace"] = _stacktrace(exception)
        attributes["exception.escaped"] = str(escaped)

    return attributes


def _stacktrace(exception: BaseException) -> str:
    # The traceback in the standard layout, formatted from a plain
    # walk of the frames. `traceback.format_exception` would also
    # compute the caret underlines beneath each source line, which on
    # Python 3.11+ means parsing every frame's source, and costs ten
    # times as much; no backend renders the underlines.

    summary = traceback.StackSummary.extract(traceback.walk_tb(exception.__traceback__))

    return "".join(
        [
            "Traceback (most recent call last):\n",
            *summary.format(),
            *traceback.format_exception_only(exception),
        ]
    )


def _status_code(result: Any) -> int | None:
    # A WSGI status line is "200 OK"; keep just the number.

    if isinstance(result, str):
        first = result.split(" ", 1)[0]
        if first.isdigit():
            return int(first)
    return None
