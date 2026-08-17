"""The observed code: a gateway charging cards.

Nothing here knows wrapture exists; the operator code in
wrapture_local/ decides what gets recorded and where events go.
"""

from __future__ import annotations


class PaymentGateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"
