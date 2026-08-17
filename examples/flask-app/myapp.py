"""A small Flask shop, unaware it is being observed.

Nothing here imports wrapture: the middleware and the handler
bindings arrive from wrapture.toml when this module is imported.
"""

from __future__ import annotations

from collections.abc import Iterator

from flask import Flask, Response, jsonify

app = Flask(__name__)

CATALOG = {"widget": 25, "gadget": 120}


def quote(item: str) -> dict[str, str | int]:
    price = CATALOG[item]
    return {"item": item, "price": price}


@app.route("/")
def index() -> Response:
    return jsonify(sorted(CATALOG))


@app.route("/quote/<item>")
def quoted(item: str) -> Response:
    return jsonify(quote(item))


@app.route("/export")
def export() -> Response:
    def rows() -> Iterator[str]:
        for item in sorted(CATALOG):
            yield f"{item},{CATALOG[item]}\n"

    return app.response_class(rows(), mimetype="text/csv")
