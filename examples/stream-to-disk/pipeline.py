"""A small extract-transform-store pipeline: the code being observed.

The sleeps stand in for real I/O, so the slices have visible width
when the trace is rendered as a timeline.
"""

from __future__ import annotations

import time


def fetch(source: str) -> list[str]:
    time.sleep(0.02)
    return [f"{source}-{item}" for item in range(3)]


def transform(item: str) -> str:
    time.sleep(0.01)
    return item.upper()


def store(item: str) -> None:
    time.sleep(0.005)


def process(source: str) -> int:
    items = fetch(source)

    for item in items:
        store(transform(item))

    return len(items)
