"""Tests for distributed trace identity.

These cover the W3C codec, minting and inheritance in the recording
path (every tree rooted in a declared operation is a trace), the kind
gate on minting, the WSGI and ASGI ingress parse, the public egress
surface (current_trace and trace_headers), the verbatim pass-through
invariant for unclaimed formats, serialisation of the identity
fields, and the [trace] config table with its per-entry re-enable.
"""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import wrapture
from wrapture import capture_logs, observed, timeline
from wrapture.sinks import _event_record
from wrapture.trace import (
    _configure,
    _parse_w3c,
    _render_w3c,
    _restore,
    from_headers,
    headers_for,
    mint,
    wanted_headers,
)

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@pytest.fixture
def trace_disabled() -> Iterator[None]:
    previous = _configure(False, ("w3c",))
    try:
        yield
    finally:
        _restore(previous)


# ---------------------------------------------------------------------------
# the W3C codec
# ---------------------------------------------------------------------------


def test_a_valid_traceparent_parses() -> None:
    slot = _parse_w3c({"traceparent": TRACEPARENT, "tracestate": "dd=s:1"})

    assert slot is not None
    assert slot.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert slot.span_id == "b7ad6b7169203331"
    assert slot.sampled is True
    assert slot.claimed is False
    assert slot.headers == {"traceparent": TRACEPARENT, "tracestate": "dd=s:1"}


def test_the_unsampled_flag_parses_false() -> None:
    unsampled = TRACEPARENT[:-2] + "00"
    slot = _parse_w3c({"traceparent": unsampled})

    assert slot is not None
    assert slot.sampled is False


@pytest.mark.parametrize(
    "value",
    [
        "",
        "junk",
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331",
        "ff-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "00-00000000000000000000000000000000-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
        "00-0af7651916cd43dd8448eb211c80319Z-b7ad6b7169203331-01",
        "00-0af7651916cd43dd-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-extra",
        "0-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    ],
)
def test_a_malformed_traceparent_is_rejected(value: str) -> None:
    assert _parse_w3c({"traceparent": value}) is None


def test_a_future_version_with_extra_fields_parses() -> None:
    future = "cc-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-what"
    slot = _parse_w3c({"traceparent": future})

    assert slot is not None
    assert slot.trace_id == "0af7651916cd43dd8448eb211c80319c"


def test_rendering_uses_the_current_ids_and_keeps_tracestate() -> None:
    slot = _parse_w3c({"traceparent": TRACEPARENT, "tracestate": "dd=s:1"})
    assert slot is not None

    slot.span_id = "aaaaaaaaaaaaaaaa"
    rendered = _render_w3c(slot)

    assert rendered["traceparent"] == (
        "00-0af7651916cd43dd8448eb211c80319c-aaaaaaaaaaaaaaaa-01"
    )
    assert rendered["tracestate"] == "dd=s:1"


def test_minted_identities_are_valid_and_distinct() -> None:
    one, two = mint(), mint()
    slot = one.slots["w3c"]

    assert len(slot.trace_id) == 32 and len(slot.span_id) == 16
    assert slot.sampled is True
    assert _parse_w3c(_render_w3c(slot)) is not None
    assert slot.trace_id != two.slots["w3c"].trace_id


def test_from_headers_with_nothing_recognised_is_none() -> None:
    assert from_headers({}) is None
    assert from_headers({"traceparent": "junk"}) is None


def test_wanted_headers_names_the_configured_formats_headers() -> None:
    assert wanted_headers() == ("traceparent", "tracestate")


# ---------------------------------------------------------------------------
# every tree is a trace
# ---------------------------------------------------------------------------


@observed
def leaf() -> dict[str, str]:
    return wrapture.trace_headers()


@observed
def branch() -> Any:
    return leaf()


def test_a_root_mints_and_children_share_the_reference() -> None:
    with timeline() as tape:
        branch()

    root, child = tape.all
    assert root.trace is not None
    assert child.trace is root.trace


def test_each_tree_is_its_own_trace() -> None:
    with timeline() as tape:
        leaf()
        leaf()

    first, second = tape.all
    assert first.trace is not None and second.trace is not None
    assert first.trace is not second.trace
    assert first.trace.slots["w3c"].trace_id != second.trace.slots["w3c"].trace_id


def test_disabled_means_no_identity_anywhere(trace_disabled: None) -> None:
    with timeline() as tape:
        branch()

    assert all(event.trace is None for event in tape.all)


def test_a_flagged_binding_mints_under_a_global_disable(
    trace_disabled: None,
) -> None:
    class Jobs:
        def run(self) -> None:
            pass

    run = wrapture.binding(Jobs, "run")
    run._trace_root = True

    with timeline(run) as tape:
        Jobs().run()

    assert tape.all[0].trace is not None


# ---------------------------------------------------------------------------
# the minting kind gate
# ---------------------------------------------------------------------------


class Settings:
    limit = 10


def test_a_root_attribute_access_never_mints() -> None:
    limit = wrapture.binding(Settings, "limit", mode="attribute")

    with timeline(limit) as tape:
        _ = Settings().limit

    event = tape.all[0]
    assert event.kind == "get"
    assert event.trace is None


def test_an_attribute_access_nested_in_a_call_inherits() -> None:
    limit = wrapture.binding(Settings, "limit", mode="attribute")

    @observed
    def read() -> int:
        return Settings().limit

    with timeline(limit) as tape:
        read()

    call, get = tape.all
    assert call.trace is not None
    assert get.trace is call.trace


def test_a_root_log_event_carries_no_trace() -> None:
    log = logging.getLogger("tracegate.root")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    logs = capture_logs("tracegate.*")

    with timeline(logs) as tape:
        log.warning("standalone line")

    event = tape.all[0]
    assert event.kind == "log"
    assert event.trace is None


def test_a_nested_log_event_shares_the_tree_trace() -> None:
    log = logging.getLogger("tracegate.nested")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    logs = capture_logs("tracegate.*")

    @observed
    def emit() -> None:
        log.warning("inside")

    with timeline(logs) as tape:
        emit()

    call, line = tape.all
    assert call.trace is not None
    assert line.trace is call.trace


# ---------------------------------------------------------------------------
# the public egress surface
# ---------------------------------------------------------------------------


def test_current_trace_and_headers_outside_recording() -> None:
    assert wrapture.current_trace() is None
    assert wrapture.trace_headers() == {}


def test_trace_headers_render_the_minted_identity() -> None:
    with timeline() as tape:
        headers = leaf()

    context = tape.all[0].trace
    assert context is not None
    slot = context.slots["w3c"]
    assert headers["traceparent"] == f"00-{slot.trace_id}-{slot.span_id}-01"


def test_an_unclaimed_arrived_slot_passes_through_verbatim() -> None:
    context = from_headers({"traceparent": TRACEPARENT, "tracestate": "dd=s:1"})
    assert context is not None

    assert headers_for(context) == {
        "traceparent": TRACEPARENT,
        "tracestate": "dd=s:1",
    }


def test_a_claimed_slot_renders_from_its_register() -> None:
    context = from_headers({"traceparent": TRACEPARENT})
    assert context is not None

    slot = context.slots["w3c"]
    slot.claimed = True
    slot.span_id = "aaaaaaaaaaaaaaaa"

    assert headers_for(context)["traceparent"] == (
        "00-0af7651916cd43dd8448eb211c80319c-aaaaaaaaaaaaaaaa-01"
    )


# ---------------------------------------------------------------------------
# ingress at the request boundary
# ---------------------------------------------------------------------------


def serve(app: Any, environ: dict[str, Any]) -> None:
    body = app(environ, lambda status, headers: None)
    list(body)
    body.close()


def test_wsgi_ingress_joins_the_incoming_trace() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b"ok"]

    wrapped = wrapture.WSGIMiddleware(app)

    with timeline() as tape:
        serve(
            wrapped,
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/x",
                "HTTP_TRACEPARENT": TRACEPARENT,
            },
        )

    request = tape.roots()[0]
    assert request.trace is not None
    slot = request.trace.slots["w3c"]
    assert slot.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert not slot.claimed


def test_wsgi_ingress_without_headers_mints() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b"ok"]

    wrapped = wrapture.WSGIMiddleware(app)

    with timeline() as tape:
        serve(wrapped, {"REQUEST_METHOD": "GET", "PATH_INFO": "/x"})

    request = tape.roots()[0]
    assert request.trace is not None
    assert request.trace.slots["w3c"].headers == {}


def test_wsgi_ingress_is_silent_when_disabled(trace_disabled: None) -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b"ok"]

    wrapped = wrapture.WSGIMiddleware(app)

    with timeline() as tape:
        serve(
            wrapped,
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/x",
                "HTTP_TRACEPARENT": TRACEPARENT,
            },
        )

    assert tape.roots()[0].trace is None


def test_a_nested_boundary_with_headers_shades_its_subtree() -> None:
    inner_calls: list[Any] = []

    def inner_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        inner_calls.append(wrapture.current_trace())
        start_response("200 OK", [])
        return [b"inner"]

    inner = wrapture.WSGIMiddleware(inner_app)

    def outer_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        serve(
            inner,
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/inner",
                "HTTP_TRACEPARENT": TRACEPARENT,
            },
        )
        start_response("200 OK", [])
        return [b"outer"]

    outer = wrapture.WSGIMiddleware(outer_app)

    with timeline() as tape:
        serve(outer, {"REQUEST_METHOD": "GET", "PATH_INFO": "/outer"})

    outer_event, inner_event = tape.roots()[0], tape.all[1]
    assert outer_event.trace is not None
    assert inner_event.trace is not None
    assert inner_event.trace is not outer_event.trace
    assert inner_event.trace.slots["w3c"].trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert inner_calls[0] is inner_event.trace


def test_a_nested_boundary_without_headers_inherits() -> None:
    def inner_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b"inner"]

    inner = wrapture.WSGIMiddleware(inner_app)

    def outer_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        serve(inner, {"REQUEST_METHOD": "GET", "PATH_INFO": "/inner"})
        start_response("200 OK", [])
        return [b"outer"]

    outer = wrapture.WSGIMiddleware(outer_app)

    with timeline() as tape:
        serve(outer, {"REQUEST_METHOD": "GET", "PATH_INFO": "/outer"})

    outer_event, inner_event = tape.roots()[0], tape.all[1]
    assert inner_event.trace is outer_event.trace


def test_asgi_scope_headers_are_lifted() -> None:
    from wrapture.asgi import _trace_scope

    scope = {
        "headers": [
            (b"host", b"example.com"),
            (b"TraceParent", TRACEPARENT.encode()),
            (b"tracestate", b"dd=s:1"),
            (b"traceparent", b"duplicate-ignored"),
        ]
    }

    assert _trace_scope(scope) == {
        "traceparent": TRACEPARENT,
        "tracestate": "dd=s:1",
    }


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------


def test_the_serialised_record_carries_identity_only() -> None:
    with timeline() as tape:
        leaf()

    context = tape.all[0].trace
    assert context is not None
    record = _event_record(tape.all[0])

    assert record["trace"] == {
        "w3c": {
            "trace_id": context.slots["w3c"].trace_id,
            "sampled": True,
        }
    }
    assert "span_id" not in str(record["trace"])


def test_no_identity_serialises_no_trace_key(trace_disabled: None) -> None:
    with timeline() as tape:
        leaf()

    assert "trace" not in _event_record(tape.all[0])


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_the_trace_table_disables_and_revert_restores(tmp_path: Path) -> None:
    source = tmp_path / "wrapture.toml"
    source.write_text("[trace]\nenabled = false\n")

    applied = wrapture.load_config(str(source)).apply()
    try:
        with timeline() as tape:
            leaf()
        assert tape.all[0].trace is None
    finally:
        applied.revert()

    with timeline() as tape:
        leaf()
    assert tape.all[0].trace is not None


def test_an_observe_entry_re_enables_under_a_disable(tmp_path: Path) -> None:
    module = tmp_path / "cfgtrace_jobs.py"
    module.write_text("def run():\n    return 'done'\n")

    source = tmp_path / "wrapture.toml"
    source.write_text(
        "[trace]\nenabled = false\n\n"
        '[[observe]]\ntarget = "cfgtrace_jobs"\nname = "run"\ntrace = true\n'
    )

    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        applied = wrapture.load_config(str(source)).apply()
        try:
            cfgtrace_jobs = __import__("cfgtrace_jobs")

            with timeline() as tape:
                cfgtrace_jobs.run()

            assert tape.all[0].trace is not None
        finally:
            applied.revert()
    finally:
        sys.path.remove(str(tmp_path))
        import sys as _sys

        _sys.modules.pop("cfgtrace_jobs", None)


def test_trace_true_on_an_attribute_only_entry_is_rejected(tmp_path: Path) -> None:
    module = tmp_path / "cfgtrace_attrs.py"
    module.write_text("threshold = 5\n")

    source = tmp_path / "wrapture.toml"
    source.write_text(
        '[[observe]]\ntarget = "cfgtrace_attrs"\nname = "threshold"\ntrace = true\n'
    )

    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        __import__("cfgtrace_attrs")

        with pytest.raises(wrapture.ConfigError, match="can never act"):
            wrapture.load_config(str(source)).apply()
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("cfgtrace_attrs", None)


def test_trace_true_on_a_mixed_entry_marks_the_operations(tmp_path: Path) -> None:
    # An entry naming both a function and a data attribute is fine:
    # the mark lands on the binding that can mint, and the attribute
    # binding is simply not marked rather than the entry rejected.

    module = tmp_path / "cfgtrace_mixed.py"
    module.write_text("def run():\n    return 'done'\n\nthreshold = 5\n")

    source = tmp_path / "wrapture.toml"
    source.write_text(
        "[trace]\nenabled = false\n\n"
        '[[observe]]\ntarget = "cfgtrace_mixed"\n'
        'name = ["run", "threshold"]\ntrace = true\n'
    )

    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        __import__("cfgtrace_mixed")

        applied = wrapture.load_config(str(source)).apply()
        try:
            cfgtrace_mixed = sys.modules["cfgtrace_mixed"]

            with timeline() as tape:
                cfgtrace_mixed.run()

            assert tape.all[0].trace is not None
        finally:
            applied.revert()
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("cfgtrace_mixed", None)


def test_a_bad_trace_table_fails_the_load(tmp_path: Path) -> None:
    bad_key = tmp_path / "key.toml"
    bad_key.write_text("[trace]\ngenerate = true\n")

    with pytest.raises(wrapture.ConfigError, match="unknown keys"):
        wrapture.load_config(str(bad_key))

    bad_format = tmp_path / "format.toml"
    bad_format.write_text('[trace]\nformats = ["zipkin9"]\n')

    with pytest.raises(wrapture.ConfigError, match="unknown trace format"):
        wrapture.load_config(str(bad_format))

    bad_flag = tmp_path / "flag.toml"
    bad_flag.write_text('[trace]\nenabled = "yes"\n')

    with pytest.raises(wrapture.ConfigError, match="true or false"):
        wrapture.load_config(str(bad_flag))
