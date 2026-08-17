"""Setup callback: binds the gateway with a per-call predicate.

Registered by the [[setup]] entry in wrapture.toml; wrapture calls
instrument(module, **options) once the shop module is imported, with
the entry's extra keys as the options. Behaviour that config cannot
spell, here the when= predicate, lives in ordinary Python like this,
while the threshold it applies stays adjustable from the config
file.
"""

from __future__ import annotations

from typing import Any

import wrapture


def instrument(module: Any, *, threshold: int = 100) -> None:
    """Observe gateway charges, recording only those over the
    threshold."""

    def large(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        return args[0] > threshold

    wrapture.binding(module.PaymentGateway, "charge", when=large).apply()
