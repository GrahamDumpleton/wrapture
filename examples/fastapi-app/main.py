"""Serve four requests through the observed FastAPI app.

Run from this directory:

    uv run --with fastapi --with httpx python -m wrapture main.py

The test client drives the full ASGI cycle, so each request prints as
one tree: the request line, the route handler and any helpers nested
beneath it, and the status line when the application coroutine
completes. The last request asks for an unknown item, so its tree
shows the KeyError escaping the handler; the client still receives
the 500 the framework makes of it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from myapp import app


def main() -> None:
    # The server error middleware re-raises after serving a 500, the
    # way a real server logs it; without raise_server_exceptions=False
    # the test client would turn that into a client-side crash.

    client = TestClient(app, raise_server_exceptions=False)

    client.get("/")
    client.get("/quote/widget")
    client.get("/export")
    client.get("/quote/missing")

    print("served 4 requests; the trees above are the trace")


if __name__ == "__main__":
    main()
