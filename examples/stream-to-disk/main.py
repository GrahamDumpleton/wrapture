"""Process several sources on a small thread pool.

Run from this directory:

    rm -f trace.jsonl
    uv run python -m wrapture main.py
    uv run python -m wrapture.tools convert --format chrome -o trace.json trace.jsonl

Then drop trace.json onto https://ui.perfetto.dev: one lane per
worker thread, nested slices per call. The trace file appends across
runs, hence deleting it first.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pipeline import process

SOURCES = ["alpha", "beta", "gamma", "delta"]


def main() -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(process, SOURCES))

    print(f"processed {sum(counts)} items from {len(SOURCES)} sources")


if __name__ == "__main__":
    main()
