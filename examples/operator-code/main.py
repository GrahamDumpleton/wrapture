"""Charge a mix of small and large amounts.

Run from this directory:

    uv run python -m wrapture main.py

The config's pythonpath entry makes the wrapture_local package next
to it importable, its setup callback binds the gateway with a when=
predicate so only charges over 100 record, and its sink factory fans
events out to a live printer and trace.jsonl at once.
"""

from __future__ import annotations

from shop import PaymentGateway

AMOUNTS = [25, 480, 60, 1250, 90, 300]


def main() -> None:
    gateway = PaymentGateway()

    for amount in AMOUNTS:
        gateway.charge(amount)

    print(f"charged {len(AMOUNTS)} amounts; only the large ones were recorded")


if __name__ == "__main__":
    main()
