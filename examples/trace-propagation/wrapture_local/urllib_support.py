"""Instrument urllib, entirely under the covers.

A stand-in for what a wrapture-probe-urllib package would ship: one
setup hook, triggered by the import of urllib.request, that binds the
opener's choke point so every outbound request both records as a
client-side call and carries the current trace identity onward in its
headers.

This is the public-tier contract in action: the probe uses only
wrapture's stable surface, a binding with a transforms_args stage and
wrapture.trace_headers(), and never touches an event. trace_headers()
returns the headers the current tree's identity should travel as,
whatever minted or parsed it, and returns nothing when nothing is
being recorded, so injection is always safe to attempt.
"""

from __future__ import annotations

from typing import Any

import wrapture


def instrument(module: Any) -> None:
    """Bind urllib's opener so outbound requests record and carry the
    trace identity."""

    def inject(
        args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if not args:
            return args, kwargs

        # OpenerDirector.open takes a URL string or a Request; the
        # headers need a Request to land on.

        target = args[0]
        if isinstance(target, str):
            target = module.Request(target)

        if isinstance(target, module.Request):
            for name, value in wrapture.trace_headers().items():
                target.add_unredirected_header(name.title(), value)

        return (target, *args[1:]), kwargs

    opener = wrapture.binding(module.OpenerDirector, "open", label="urllib.open")
    opener.on_call.transforms_args(inject).apply()
