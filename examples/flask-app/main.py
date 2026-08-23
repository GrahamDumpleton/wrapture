"""Serve five requests through the observed Flask app.

Run from this directory:

    uv run --with flask python -m wrapture main.py

The test client drives the full WSGI cycle, so each request prints as
one tree: the request line, the view handler and any helpers nested
beneath it, and the status line when the body closes. The gadget
quote logs a big-ticket warning, and the last request asks for an
unknown item, so its tree shows the KeyError escaping the view and,
on the request's own closing line, the 500 it becomes with the
KeyError noted beside it.
"""

from __future__ import annotations

from flask.testing import FlaskClient

from myapp import app


def fetch(client: FlaskClient, url: str) -> None:
    """Play the server's part in full: consume the body, then close it.

    The closing line of a request prints when its body closes. A
    response left unconsumed would leave its request event visibly
    open, exactly as a real server that never closed the iterable
    would.
    """

    reply = client.get(url)
    reply.get_data()
    reply.close()


def main() -> None:
    client = app.test_client()

    fetch(client, "/")
    fetch(client, "/quote/widget")
    fetch(client, "/quote/gadget")
    fetch(client, "/export")
    fetch(client, "/quote/missing")

    print("served 5 requests; the trees above are the trace")


if __name__ == "__main__":
    main()
