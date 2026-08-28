"""Distributed trace identity: the context events carry across services.

wrapture's own event linkage (seq and parent_id) is process local. A
trace identity extends the tree across processes: a request arriving
with a W3C `traceparent` header joins the caller's distributed trace,
a tree starting locally mints an identity of its own, and outbound
requests carry the identity onward, so two services both observed by
wrapture join their trace files on one id, with or without any
tracing backend involved.

Every tree is a trace. With the mechanism enabled (it is by default),
any root event that inherited no context mints one, and the WSGI and
ASGI middleware are the special case that parses incoming headers
first, minting only when none arrived. Children share their parent's
context by reference: the identity fields are written once, at the
boundary, before anything can read them, and the one field that
changes afterwards, the span-id register a tracing sink maintains, is
runtime plumbing for outbound injection, never serialised.

This module holds the vocabulary: the context and slot types, the
W3C trace context codec, minting, and the header rendering outbound
injection uses. W3C trace context is the one wire format wrapture
speaks; the ecosystem has converged on it, and vendor baggage rides
inside its own `tracestate` header, so other conventions pass
through the application without wrapture's involvement at all. It
deliberately knows no vendor SDK: the codec is a public wire
protocol, the same kind of boundary knowledge the WSGI middleware
embeds. The process-wide switch lives here too, configured by the
`[trace]` config table.

The contract has two tiers. Instrumentation packages, which inject
headers into outbound requests, use only `wrapture.current_trace()`
and `wrapture.trace_headers()`, a surface that will hold stable.
Tracing sinks, which overwrite a slot's span id with the ids they
actually export, reach into the slot directly; that is internals
territory, co-maintained with wrapture itself.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass
class TraceSlot:
    """One wire format's view of the trace: the identity as that
    format spells it, and the raw headers it arrived in.

    `trace_id` and `span_id` are lowercase hex, in the format's own
    widths. `span_id` is a register, not a fact: it holds the parent
    id outbound injection should send, and a tracing sink that
    exports real spans overwrites it (and sets `claimed`) so
    downstream services parent onto spans that exist. An unclaimed
    slot that arrived in headers is re-injected from `headers`
    verbatim: wrapture never breaks a trace it does not understand,
    it passes it through as a transparent hop.
    """

    trace_id: str
    span_id: str
    sampled: bool | None = None
    headers: dict[str, str] = field(default_factory=dict)
    claimed: bool = False


@dataclass
class TraceContext:
    """The distributed trace identity a tree of events carries.

    One slot, keyed by format name with "w3c" the only occupant: the
    keyed shape is deliberate, keeping the serialised form stable,
    but w3c is the one format wrapture speaks. Every event in a tree
    shares one TraceContext by reference; a nested boundary that
    receives its own headers starts a fresh one for its subtree.
    """

    slots: dict[str, TraceSlot] = field(default_factory=dict)


# -- the W3C trace context codec --------------------------------------------

_W3C_TRACEPARENT = "traceparent"
_W3C_TRACESTATE = "tracestate"


def _is_hex(value: str) -> bool:
    return all(c in "0123456789abcdef" for c in value)


def _parse_w3c(headers: Mapping[str, str]) -> TraceSlot | None:
    # Parse a traceparent per the W3C recommendation, strictly enough
    # to reject garbage and loosely enough to accept future versions:
    # version "ff" is forbidden, version "00" must have exactly four
    # fields, and later versions may carry more.

    value = headers.get(_W3C_TRACEPARENT, "").strip()
    if not value:
        return None

    fields = value.split("-")
    if len(fields) < 4:
        return None

    version, trace_id, span_id, flags = fields[:4]

    if len(version) != 2 or not _is_hex(version) or version == "ff":
        return None
    if version == "00" and len(fields) != 4:
        return None

    if len(trace_id) != 32 or not _is_hex(trace_id) or trace_id == "0" * 32:
        return None
    if len(span_id) != 16 or not _is_hex(span_id) or span_id == "0" * 16:
        return None
    if len(flags) != 2 or not _is_hex(flags):
        return None

    raw = {_W3C_TRACEPARENT: value}
    tracestate = headers.get(_W3C_TRACESTATE, "").strip()
    if tracestate:
        raw[_W3C_TRACESTATE] = tracestate

    return TraceSlot(
        trace_id=trace_id,
        span_id=span_id,
        sampled=bool(int(flags, 16) & 0x01),
        headers=raw,
    )


def _render_w3c(slot: TraceSlot) -> dict[str, str]:
    # Render the slot's current ids as headers. Used for claimed and
    # minted slots; a slot that arrived in headers and was never
    # claimed is forwarded from its raw headers instead.

    flags = "01" if slot.sampled else "00"
    headers = {_W3C_TRACEPARENT: f"00-{slot.trace_id}-{slot.span_id}-{flags}"}

    tracestate = slot.headers.get(_W3C_TRACESTATE)
    if tracestate:
        headers[_W3C_TRACESTATE] = tracestate

    return headers


def _mint_w3c() -> TraceSlot:
    # A fresh identity: random 128-bit trace id, random 64-bit span
    # id for the root, sampled, since a locally minted trace exists
    # because somebody wanted it.
    #
    # The ids only need to be unique, not unguessable, so they come
    # from the random module, chosen over secrets because this runs
    # on every root event and secrets costs a urandom syscall per id.
    # The random module reseeds itself after fork, so children of a
    # forked worker do not repeat the parent's sequence.

    return TraceSlot(
        trace_id=f"{random.getrandbits(128):032x}",
        span_id=f"{random.getrandbits(64):016x}",
        sampled=True,
    )


def wanted_headers() -> tuple[str, ...]:
    """The casefolded request header names the trace parse reads,
    for the middleware to lift off a request."""

    return (_W3C_TRACEPARENT, _W3C_TRACESTATE)


# -- the process-wide switch, configured by the [trace] config table --------

_state_lock = threading.Lock()
_enabled = True


def _configure(enabled: bool) -> bool:
    """Set the process-wide trace switch, returning the previous
    value so a reverted config can restore it."""

    global _enabled

    with _state_lock:
        previous = _enabled
        _enabled = enabled

    return previous


def _restore(previous: bool) -> None:
    global _enabled

    with _state_lock:
        _enabled = previous


def _active() -> bool:
    return _enabled


# -- the operations the recording path and the middleware use ---------------


def from_headers(headers: Mapping[str, str]) -> TraceContext | None:
    """Parse incoming request headers into a TraceContext, or None
    when no `traceparent` is present.

    `headers` maps casefolded header names to values; the middleware
    builds it from the environ or the scope. The raw headers are kept
    on the slot for verbatim re-injection.
    """

    slot = _parse_w3c(headers)
    if slot is None:
        return None

    return TraceContext(slots={"w3c": slot})


def mint() -> TraceContext:
    """A fresh locally originated identity, in the canonical W3C
    format."""

    return TraceContext(slots={"w3c": _mint_w3c()})


def headers_for(context: TraceContext) -> dict[str, str]:
    """The headers outbound injection should send for a context.

    A claimed or minted slot renders from its current ids, so the
    span-id register a tracing sink maintains is what downstream
    parents onto. A slot that arrived in headers and was never
    claimed forwards those headers verbatim: that product sees this
    service as a transparent hop and its trace stays connected.
    """

    headers: dict[str, str] = {}

    for slot in context.slots.values():
        if not slot.claimed and slot.headers:
            headers.update(slot.headers)
        else:
            headers.update(_render_w3c(slot))

    return headers


__all__ = ["TraceContext", "TraceSlot", "from_headers", "headers_for", "mint"]
