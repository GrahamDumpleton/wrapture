"""Sink factory: composes what the [sink] table alone cannot spell.

The type key in wrapture.toml names this factory by module:attr
reference; the remaining table keys arrive as keyword arguments, and
the returned sink is registered as the process sink.
"""

from __future__ import annotations

import wrapture


def make_sink(path: str) -> wrapture.Sink:
    """Fan events out to a live printer and a JSONLines file."""

    return wrapture.Fanout(wrapture.Printer(), wrapture.JSONLines(path))
