"""OTel environment handling: config-supplied defaults and the
protocol selection the standard variables drive."""

from __future__ import annotations

import os
from typing import Any


def _apply_environment(environment: dict[str, Any]) -> None:
    """Apply `environment` table keys as OTel environment defaults.

    Mechanical mapping rather than named options: uppercase the key,
    prefix OTEL_ when missing, stringify the value, setdefault. The
    config file thereby supplies defaults for any of the SDK's
    documented variables, while a variable set in the real
    environment always wins.
    """

    for key, value in environment.items():
        name = key.upper()
        if not name.startswith("OTEL_"):
            name = f"OTEL_{name}"

        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)

        os.environ.setdefault(name, text)


def _otlp_protocol(signal_variable: str) -> str:
    # The signal-specific protocol variable wins over the general one,
    # matching the OTel SDK's own precedence.

    return os.environ.get(
        signal_variable,
        os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    )
