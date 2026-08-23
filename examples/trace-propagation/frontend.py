"""The client half: orders quotes over HTTP.

Nothing here mentions trace headers. The client.toml next to it
observes these functions, and its setup hook instruments urllib so
every outbound request carries the current trace identity; the
minted id then reappears in the server's records.

The one piece of embedded instrumentation is the pair of blocks in
fetch_quote, splitting the exchange into making the request (which
returns once the response status and headers arrive) and consuming
the reply body, so the two phases show separately in a trace.
Blocks are inert when nothing is recording, so the module still runs
unobserved.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import urlopen

import wrapture

BASE = "http://127.0.0.1:8309"


def fetch_quote(item: str) -> dict[str, object]:
    with wrapture.block("request-quote"):
        reply = urlopen(f"{BASE}/quote/{item}")

    with wrapture.block("consume-reply"), reply:
        return json.loads(reply.read())


def place_order(item: str) -> str:
    try:
        payload = fetch_quote(item)
    except HTTPError as error:
        return f"{item}: no quote ({error.code})"

    return f"{item}: {payload['price']}"
