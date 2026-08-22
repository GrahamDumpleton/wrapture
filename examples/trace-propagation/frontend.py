"""The client half: orders quotes over HTTP, unaware it is observed.

Nothing here imports wrapture, and nothing here mentions trace
headers. The client.toml next to it observes these functions, and its
setup hook instruments urllib so every outbound request carries the
current trace identity; the minted id then reappears in the server's
records.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import urlopen

BASE = "http://127.0.0.1:8309"


def fetch_quote(item: str) -> dict[str, object]:
    with urlopen(f"{BASE}/quote/{item}") as reply:
        return json.loads(reply.read())


def place_order(item: str) -> str:
    try:
        payload = fetch_quote(item)
    except HTTPError as error:
        return f"{item}: no quote ({error.code})"

    return f"{item}: {payload['price']}"
