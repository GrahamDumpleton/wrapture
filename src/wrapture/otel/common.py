"""Shared vocabulary for the OpenTelemetry sink modules."""

from __future__ import annotations

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


def _exception_attributes(exception: BaseException, level: str) -> dict[str, Any]:
    # The semconv exception attributes for a reduced `exceptions=`
    # level: the SDK's own record_exception() always formats the
    # stacktrace, so a sink at "message" or "type" builds the event
    # itself from this.

    attributes: dict[str, Any] = {"exception.type": type(exception).__name__}
    if level == "message":
        attributes["exception.message"] = str(exception)

    return attributes


def _status_code(result: Any) -> int | None:
    # A WSGI status line is "200 OK"; keep just the number.

    if isinstance(result, str):
        first = result.split(" ", 1)[0]
        if first.isdigit():
            return int(first)
    return None
