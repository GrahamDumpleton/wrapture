"""Shared vocabulary for the OpenTelemetry sink modules."""

from __future__ import annotations

from typing import Any

# Attribute values OTel accepts natively. Anything else is stringified.

_PRIMITIVES = (bool, int, float, str)

# The namespace wrapture-specific attribute names live under, for
# spans and metrics alike.

_PREFIX = "wrapture"

# Request data fields already exported under their semantic-convention
# names, so the close-time sweep does not repeat them as wrapture.data.*.

_SEMCONV_DATA = frozenset({"method", "path", "query"})


def _status_code(result: Any) -> int | None:
    # A WSGI status line is "200 OK"; keep just the number.

    if isinstance(result, str):
        first = result.split(" ", 1)[0]
        if first.isdigit():
            return int(first)
    return None
