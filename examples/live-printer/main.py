"""Place a few orders, one of which is declined.

Run from this directory:

    uv run python -m wrapture main.py

The printer sink writes the call tree live to stderr as each order
is placed, while this script itself never mentions wrapture.
"""

from __future__ import annotations

from shop import OrderService, PaymentDeclinedError

ORDERS = [
    ("order-1", 30, "5100-0010"),
    ("order-2", 240, "5100-0020"),
    ("order-3", 75, "4000-0030"),
]


def main() -> None:
    service = OrderService()

    for order_id, amount, card in ORDERS:
        try:
            receipt = service.place(order_id, amount, card)
            print(f"{order_id}: paid, receipt {receipt}")
        except PaymentDeclinedError as declined:
            print(f"{order_id}: {declined}")


if __name__ == "__main__":
    main()
