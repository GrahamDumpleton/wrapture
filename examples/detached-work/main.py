"""Three uploads, each handing work to a pool and to a queue.

Run from this directory, with a collector or a viewer listening on
localhost:4318 (otel-desktop-viewer, for instance):

    uv run --extra otel python -m wrapture --config wrapture.toml main.py

Each upload produces three traces: the request, the thumbnail work
on a pool thread with a link back to the request, and the
notification on the consumer thread with a remote-style link back
to the same request, carried by the message's headers. With no
viewer to hand, OTEL_TRACES_EXPORTER=console prints the spans, links
included, to standard output instead.
"""

from __future__ import annotations

import time

from uploads import handle_upload, start_notifier, stop_notifier


def main() -> None:
    notifier = start_notifier()

    try:
        for name in ("cat.png", "dog.png", "fish.png"):
            print(f"{name}: {handle_upload(name)}")
            time.sleep(0.05)
    finally:
        stop_notifier(notifier)


if __name__ == "__main__":
    main()
