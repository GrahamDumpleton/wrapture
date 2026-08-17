"""A small FastAPI shop, unaware it is being observed.

Nothing here imports wrapture: the middleware and the handler
observers arrive from wrapture.toml when this module is imported.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI(title="myapp")

CATALOG = {"widget": 25, "gadget": 120}


def quote(item: str) -> dict[str, str | int]:
    price = CATALOG[item]
    return {"item": item, "price": price}


@app.get("/")
def index() -> list[str]:
    return sorted(CATALOG)


@app.get("/quote/{item}")
async def quoted(item: str) -> dict[str, Any]:
    return quote(item)


@app.get("/export")
def export() -> StreamingResponse:
    def rows() -> Iterator[str]:
        for item in sorted(CATALOG):
            yield f"{item},{CATALOG[item]}\n"

    return StreamingResponse(rows(), media_type="text/csv")
