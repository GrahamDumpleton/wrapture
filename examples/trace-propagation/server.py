"""Serve the quote service. Run from this directory, first terminal:

    uv run python -m wrapture --config server.toml server.py

Each request the client sends prints as one tree here, carrying the
same trace id the client minted; server.jsonl records the events with
the id on every line. Stop with Ctrl-C.
"""

from __future__ import annotations

from wsgiref.simple_server import WSGIRequestHandler, make_server

from backend import app


class QuietHandler(WSGIRequestHandler):
    """Suppress the default per-request access log line, so the
    printed call trees are the output."""

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    with make_server("127.0.0.1", 8309, app, handler_class=QuietHandler) as server:
        print("quote service on http://127.0.0.1:8309, Ctrl-C to stop")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
