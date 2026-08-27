"""An upload service that hands work off rather than waiting for it.

Two hand-offs, two shapes of link. handle_upload() stores the file
and fans thumbnail generation out to a thread pool with detach(),
returning before the thumbnails exist: the thumbnail work records as
its own tree, linked back to the request that started it, and the
request's duration is its own. It also enqueues a notification: the
producer captures the in-flight event with handoff() and puts its
headers on the message, and the consumer thread's block(links=...)
names those headers, so the notification tree links back to the
request across what would, with a real broker, be a process
boundary.

Nothing here imports wrapture apart from those two calls; every
event comes from the [[observe]] entries in wrapture.toml.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import wrapture

SIZES = (32, 64, 128)

pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumbnails")
notifications: queue.Queue[dict[str, Any] | None] = queue.Queue()


def handle_upload(name: str) -> str:
    store(name)

    # Fire and forget: the request returns while the thumbnails are
    # still being made, so their work is linked to this request, not
    # nested inside it.

    pool.submit(wrapture.detach(generate_thumbnails), name)

    # A message for the notifier. The origin captured here travels
    # on the message as headers, the way it would through a broker.

    origin = wrapture.handoff()
    notifications.put(
        {"name": name, "headers": origin.headers() if origin is not None else {}}
    )

    return "accepted"


def store(name: str) -> None:
    time.sleep(0.01)


def generate_thumbnails(name: str) -> list[str]:
    return [resize(name, size) for size in SIZES]


def resize(name: str, size: int) -> str:
    time.sleep(0.02)
    return f"{name}@{size}"


def notify(name: str) -> None:
    time.sleep(0.005)


def notifier() -> None:
    # The consumer loop: each message becomes a root block linked to
    # the producer's request by the headers it carried.

    while True:
        message = notifications.get()
        if message is None:
            return

        with wrapture.block("notify-upload", links=[message["headers"]]):
            notify(message["name"])


def start_notifier() -> threading.Thread:
    thread = threading.Thread(target=notifier, name="notifier")
    thread.start()
    return thread


def stop_notifier(thread: threading.Thread) -> None:
    notifications.put(None)
    thread.join()
    pool.shutdown(wait=True)
