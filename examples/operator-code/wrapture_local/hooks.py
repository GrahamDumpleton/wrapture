"""Observe gateway charges over a threshold.

Named by the [[instrument]] entry in wrapture.toml; wrapture imports
this module when the config loads (it imports only wrapture) and
calls apply() once the shop module is imported, with the entry's
extra keys resolved into self.settings. Behaviour that config cannot
spell, here the when= predicate, lives in ordinary Python like this,
while the threshold it applies stays adjustable from the file, and
the settings declaration is what makes a misspelt key a loud error
at load rather than a silently ignored one.
"""

from __future__ import annotations

from typing import Any

import wrapture


class ShopInstrumentation(wrapture.Instrumentation):
    """Record gateway charges over a configurable threshold."""

    target = "shop"
    modules = ("shop",)
    removable = True
    settings = {
        "threshold": wrapture.Setting(
            100, "charges at or below this are not recorded"
        ),
    }

    def apply(self, name: str, module: Any) -> None:
        threshold = self.settings["threshold"]

        def large(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
            return args[0] > threshold

        charge = wrapture.binding(module.PaymentGateway, "charge", when=large)
        charge.apply()

        self.on_remove(charge.remove)
