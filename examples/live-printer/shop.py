"""A small shop domain: the code being observed.

Nothing in this module knows wrapture exists. The wrapture.toml next
to it names the members to observe, and the runner applies that
config before main.py imports this module. The one warning logged
here goes through ordinary stdlib logging; the config's [[log]] entry
is what puts it into the trace.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("shop.orders")


class PaymentDeclinedError(Exception):
    """The card was declined by the payment provider."""


class PaymentGateway:
    def charge(self, amount: int, card: str) -> str:
        if card.startswith("4000"):
            raise PaymentDeclinedError(f"card {card} declined")

        return f"ch_{amount}"


class Ledger:
    def record(self, order_id: str, amount: int) -> str:
        return f"ledger:{order_id}:{amount}"


class OrderService:
    def __init__(self) -> None:
        self.gateway = PaymentGateway()
        self.ledger = Ledger()

    def place(self, order_id: str, amount: int, card: str) -> str:
        try:
            receipt = self.gateway.charge(amount, card)
        except PaymentDeclinedError:
            logger.warning("order %s declined", order_id)
            raise

        self.ledger.record(order_id, amount)

        return receipt
