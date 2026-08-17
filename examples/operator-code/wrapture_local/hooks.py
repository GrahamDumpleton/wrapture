"""Setup callback: binds the gateway with a per-call predicate.

Registered by the [[setup]] entry in wrapture.toml; wrapture calls
instrument(module) once the shop module is imported. Behaviour that
config cannot spell, here the when= predicate, lives in ordinary
Python like this.
"""

from __future__ import annotations

from typing import Any

import wrapture


def _large(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    return args[0] > 100


def instrument(module: Any) -> None:
    """Observe gateway charges, recording only the ones over 100."""

    wrapture.binding(module.PaymentGateway, "charge", when=_large).apply()
