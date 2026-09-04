"""The ASGI middleware behind mode="asgi" bindings.

An ASGI application's calling convention defeats a plain call wrapper
just as WSGI's does, only through different plumbing: the status and
headers travel through the send channel as an http.response.start
message rather than the return value, the body is a sequence of
http.response.body messages the application awaits out one at a time,
and a client disconnect surfaces as a message on the receive channel.
The middleware here interposes on both channels, forwarding every
message unaltered and unbuffered, passing non-HTTP scopes through
untouched, and awaiting the application directly when nothing is
recording.

Inside a recording scope each request records one event of kind
"request", the same shape the WSGI middleware records: duration is
wall time from the call to the completion of the application
coroutine (the response is over when the coroutine returns, so this
is time to last byte), data["app_duration"] is the time until the
http.response.start message (how long before the response started),
body_duration is the streaming tail from there to the final body
message, and items counts the body messages. The status line is
synthesised from the integer status as the event's result, and the
HTTP details ride in event.data, never in event.path, which stays the
bound location.

Everything the application does happens inside its coroutine, which
is awaited with the event already pushed, so nested bindings link
beneath the request through the streaming tail with no re-push
machinery: awaiting does not switch contextvars context, and
concurrent requests run in separate server tasks with separate
context copies.

Bodies sent through server extensions (http.response.pathsend, zero
copy send) are forwarded but not observed: they are not counted in
items or bytes, the pathsend analogue of the WSGI write() limitation.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from wrapt import MISSING, CallableObjectProxy

from . import trace as _trace
from .capture import (
    _REDACTED,
    NONE,
    CapturePolicy,
    _capture_value,
    _level_of,
    _resolve_policy,
    capture_query,
)
from .events import Event, _check_category
from .filters import _scope_fields
from .sinks import (
    _active_sinks,
    _in_recorder,
    _record_event,
    _required_policy,
)
from .stacks import _capture as _capture_stack
from .timeline import (
    _SILENCE_ALL,
    _SILENCE_SPANS,
    _check_leaf,
    _check_tree,
    _hide,
    _pop,
    _push,
    _silence,
    _suppressed,
    _timelines_active,
    _unhide,
    seed_data,
)
from .wsgi import _describe, _Hooks, _request_predicate

if TYPE_CHECKING:
    from .bindings import Binding

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApplication = Callable[[Scope, Receive, Send], Awaitable[Any]]


def _status_line(status: Any) -> str:
    # The "200 OK" form of the integer status, since ASGI carries no
    # reason phrase; an unrecognised code falls back to the bare
    # number, so Printer and exporters still see the outcome.

    try:
        return f"{status} {HTTPStatus(status).phrase}"
    except (ValueError, TypeError):
        return str(status)


def _status_int(status: Any) -> Any:
    # The integer form for the http.response.start message. A
    # "200 OK" style string is accepted by taking its leading integer,
    # the convenience mirror of the bare-bytes body wrap; anything
    # else forwards as given, no type policing.

    if isinstance(status, str):
        head, _, _ = status.partition(" ")
        try:
            return int(head)
        except ValueError:
            return status

    return status


def _encoded_headers(headers: Iterable[tuple[Any, Any]]) -> list[tuple[Any, Any]]:
    # The byte pairs the message wants, from the (str, str) pairs
    # returns() accepts; pairs already in bytes pass through.

    encoded: list[tuple[Any, Any]] = []

    for name, value in headers:
        if isinstance(name, str):
            name = name.encode("latin-1")
        if isinstance(value, str):
            value = value.encode("latin-1")
        encoded.append((name, value))

    return encoded


def _trace_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    # The trace propagation headers, lifted off the scope's byte-pair
    # header list under their casefolded names. First occurrence
    # wins, matching how the WSGI environ collapses repeats.

    wanted = _trace.wanted_headers()
    headers: dict[str, str] = {}

    for raw_name, raw_value in scope.get("headers") or ():
        try:
            name = bytes(raw_name).decode("latin-1").casefold()
        except (TypeError, ValueError):
            continue

        if name in wanted and name not in headers:
            try:
                headers[name] = bytes(raw_value).decode("latin-1")
            except (TypeError, ValueError):
                continue

    return headers


def _scope_data(scope: Mapping[str, Any], policy: CapturePolicy) -> dict[str, Any]:
    # The scope subset recorded on the event, the ASGI sources for the
    # same fields the WSGI middleware records. Values are captured at
    # the policy's level but under no name: by-name redaction pertains
    # to query string parameters only, matched inside capture_query(),
    # never to these fields. scope["path"] is already the full decoded
    # path, and the query string is separate by construction.

    data: dict[str, Any] = {"interface": "asgi"}

    for name, value in _scope_fields(scope).items():
        if value:
            data[name] = _capture_value(policy, None, value)

    # The query string is bytes on the wire; a value that cannot be
    # decoded records the marker wholesale, never raw.

    query_string = scope.get("query_string", b"")
    if query_string:
        try:
            query = (
                query_string.decode("latin-1")
                if isinstance(query_string, bytes)
                else str(query_string)
            )
        except Exception:
            data["query"] = _REDACTED
        else:
            data["query"] = capture_query(query, policy)

    return data


def _content_headers(event: Event, headers: Iterable[Any]) -> None:
    # Lift the two headers worth recording by default off the
    # http.response.start message, decoding the byte pairs; a header
    # that does not decode is simply not recorded.

    for header in headers:
        try:
            name, value = header
            folded = (
                name.decode("latin-1") if isinstance(name, bytes) else str(name)
            ).casefold()
            text = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        except Exception:
            continue

        if folded == "content-type":
            event.data["content_type"] = text
        elif folded == "content-length":
            try:
                event.data["content_length"] = int(text)
            except ValueError:
                event.data["content_length"] = text


class ASGIMiddleware(CallableObjectProxy[Any]):
    """ASGI middleware that records each request as one event.

    Wraps an ASGI 3 application and is itself one. Usable standalone,
    wherever an application object is handed to a server:

        application = ASGIMiddleware(application)

    or installed at a named attribute by a mode="asgi" binding, which
    adds the binding lifecycle (apply/remove, suspend, when=, config
    reachability) and the on_request behaviour namespace on top.

    Only HTTP scopes are observed; websocket and lifespan scopes pass
    through completely untouched. When nothing is listening the
    application is awaited with the original channels. When recording,
    the request records one "request" event: the status line as its
    result, method, path, query (redacted by parameter name), scheme
    and peer in its data, body message count and streaming timing as
    iteration fields, and every binding that fires while handling the
    request nested beneath it.

    The standalone constructor carries the recording options a
    mode="asgi" binding would otherwise supply; an explicit option
    wins over the bound binding's value.

    - `when` decides per request whether to record, behaviour still
      applying when it declines: a callable taking the scope and
      returning a boolean, or a filter_requests() filter over the
      request's recorded fields. Booleans pass as with when= elsewhere:
      False never records.

    - `tree` extends a decline to everything beneath the request:
      with tree=True nothing that fires while a declined request is
      served records, where by default a declined request leaves its
      inner operations recording as roots of their own. It needs a
      when= to act on.

    - `capture_args` is the capture policy for the request's
      descriptive data, the query string foremost, where redact()
      masks parameters by name; `capture_result` the policy for the
      status-line result.

    - `leaf` and `category` declare what the application is, as on a
      binding: a leaf request records its own event and silences the
      spans beneath it, and a category names the kind of operation.
    """

    def __init__(
        self,
        application: ASGIApplication,
        *,
        binding: Binding | None = None,
        label: str | None = None,
        when: Any = None,
        tree: bool = False,
        leaf: bool = False,
        category: str | None = None,
        capture_args: CapturePolicy | str | None = None,
        capture_result: CapturePolicy | str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(application)

        # A mode binding is refused a resolver at construction, so its
        # label, category and data are the static forms; the checks
        # here say so to the type checker, and hold for a call binding
        # handed over by a package building the middleware itself.

        if binding is not None:
            path = binding._path
            if label is None and isinstance(binding._label, str):
                label = binding._label
        else:
            path = _describe(application)

        self._self_wrapture_binding = binding
        self._self_path = path
        self._self_label = label

        # The standalone options mirror what a mode="asgi" binding
        # carries, for the middleware an instrumentation package builds
        # itself: an explicit option wins, else the bound binding's
        # value, else the default. when= is normalized here to the
        # internal convention, so __call__ treats both sources alike.

        self._self_when = _request_predicate(when, "asgi")
        self._self_tree = _check_tree(tree, self._self_when)
        self._self_leaf = _check_leaf(leaf) or (binding._leaf if binding else False)
        self._self_category = _check_category(category)
        if (
            self._self_category is None
            and binding is not None
            and isinstance(binding._category, str)
        ):
            self._self_category = binding._category

        self._self_capture_args = _resolve_policy(capture_args)
        self._self_capture_result = _resolve_policy(capture_result)
        self._self_data = seed_data(data) or (
            binding._data
            if binding is not None and isinstance(binding._data, dict)
            else {}
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> Any:
        binding = self._self_wrapture_binding
        application = self.__wrapped__

        # Only HTTP requests are observed. Everything else, websocket
        # and lifespan scopes included, must still reach the
        # application, since the server sends every connection through
        # the app it was given.

        if scope.get("type") != "http":
            return await application(scope, receive, send)

        # A suspended binding is inert: straight through, counted.

        if binding is not None and not binding._enabled():
            return await application(scope, receive, send)

        hooks = _Hooks(binding)

        # when=False makes this a behaviour-only binding: it never
        # records, counts nothing, and takes no part in gap detection.

        when = self._self_when
        tree = self._self_tree
        if when is None and binding is not None:
            when = binding._when
            tree = binding._tree

        active = _active_sinks()

        # A boundary inside a boundary is the same request seen twice:
        # an enclosing middleware marked the scope, so this copy
        # records nothing of its own and the outermost wins. Behaviour
        # still applies. A genuine sub-request arrives with a fresh
        # scope and records as usual.

        duplicate = bool(scope.get("wrapture.request"))

        recording = (
            when is not False
            and bool(active)
            and not _in_recorder.get()
            and not duplicate
        )

        if (
            when is not False
            and not duplicate
            and not recording
            and not _in_recorder.get()
            and _timelines_active()
        ):
            if binding is not None:
                binding._note_missed_call()

        # The per-request predicate decides recording only; behaviour
        # still applies when it declines, matching when= elsewhere. A
        # request beneath an operation a tree=True binding declined is
        # declined without consulting anything, and a decline here
        # with tree=True silences everything beneath this request.

        silenced = when is False and tree and bool(active)

        # A request that would have recorded but is silenced beneath a
        # leaf, or declined per request, is hidden from current_event()
        # for its extent, as a silenced or declined call is.

        hidden_request = False

        if recording and _suppressed.get():
            if binding is not None and _suppressed.get() >= _SILENCE_ALL:
                binding._filtered_calls += 1
            recording = False
            hidden_request = True
        elif recording and callable(when):
            guard = _in_recorder.set(True)
            try:
                wanted = when(None, (scope,), {})
            finally:
                _in_recorder.reset(guard)

            if not wanted:
                if binding is not None:
                    binding._filtered_calls += 1
                recording = False
                silenced = tree
                hidden_request = True

        # The untouched fast path: nothing recording and no behaviour
        # configured means the application sees the original channels.
        # A silenced request is awaited with recording suppressed
        # beneath it instead; the application does all its work inside
        # this await, streaming included.

        if not recording and not hooks.configured():
            hidden = _hide() if hidden_request else None
            silence = _silence(_SILENCE_ALL) if silenced else None
            try:
                return await application(scope, receive, send)
            finally:
                if silence is not None:
                    _suppressed.reset(silence)
                _unhide(hidden)

        # Inbound stages see and may replace the scope before the
        # application (or a terminal) does.

        for stage in hooks.inbound:
            scope = stage(scope)

        event: Event | None = None

        if recording:
            args_policy = self._self_capture_args
            if args_policy is None and binding is not None:
                args_policy = binding._capture_args
            if args_policy is None:
                args_policy = _required_policy(active, "capture_args")

            guard = _in_recorder.set(True)
            try:
                event = Event(
                    "request",
                    self._self_path,
                    label=self._self_label,
                    category=self._self_category,
                    binding=binding,
                    capture=_level_of(args_policy),
                    injected=(
                        binding._injects.get("request", False)
                        if binding is not None
                        else False
                    ),
                )

                if binding is not None and binding._stack_depth is not None:
                    event.stack = _capture_stack(binding._stack_depth)

                # The declared seed goes in first, so the request's own
                # fields, written next, win over any reserved key a
                # declaration named.

                if self._self_data:
                    event.data.update(self._self_data)

                if _level_of(args_policy) > NONE:
                    event.data.update(_scope_data(scope, args_policy))
                else:
                    event.data["interface"] = "asgi"

                # Incoming trace context, parsed at the boundary: the
                # request joins the caller's distributed trace. With
                # no recognised headers the event's trace stays None
                # and the recording path inherits or mints as usual;
                # with the mechanism disabled (and this binding not
                # re-enabling it) nothing is parsed at all.

                if _trace._active() or (
                    binding is not None and getattr(binding, "_trace_root", False)
                ):
                    event.trace = _trace.from_headers(_trace_scope(scope))
            finally:
                _in_recorder.reset(guard)

        # Mark the request as recorded, so a nested copy of the
        # middleware handed this same scope passes it through
        # rather than recording it twice.

        if event is not None:
            scope["wrapture.request"] = True

        # Response progress shared between the channel wrappers and
        # the close below: the status once http.response.start has
        # gone through, chunk counts, and whether the final body
        # message (or an extension that implies it) was seen.

        state: dict[str, Any] = {
            "started": None,
            "status": None,
            "response_at": None,
            "last_body_at": None,
            "items": 0,
            "bytes": 0,
            "complete": False,
        }

        async def sending(message: Message) -> None:
            kind = message.get("type")

            if kind == "http.response.start":
                status = message.get("status")
                headers = message.get("headers", [])

                for stage in hooks.response:
                    status, headers = stage(status, headers)
                if hooks.response:
                    message = dict(message, status=status, headers=list(headers))

                now = time.perf_counter()
                state["status"] = status
                state["response_at"] = now

                if event is not None:
                    if state["started"] is not None:
                        event.data["app_duration"] = now - state["started"]
                    _content_headers(event, headers)

            elif kind == "http.response.body":
                body = message.get("body", b"")

                for stage in hooks.body:
                    body = stage(body)
                if hooks.body:
                    message = dict(message, body=body)

                state["items"] += 1
                state["last_body_at"] = time.perf_counter()
                if not message.get("more_body"):
                    state["complete"] = True

                if event is not None:
                    event.items = state["items"]
                    try:
                        state["bytes"] += len(body)
                    except TypeError:
                        pass
                    else:
                        event.data["bytes"] = state["bytes"]

            # Extension messages forward untouched, but the two known
            # body-carrying ones still settle completeness, so a
            # response sent that way is not misrecorded as incomplete;
            # its content stays uncounted, the documented limitation.

            elif kind == "http.response.pathsend":
                state["complete"] = True
            elif kind == "http.response.zerocopysend":
                if not message.get("more_body"):
                    state["complete"] = True

            await send(message)

        async def receiving() -> Message:
            message = await receive()

            # A disconnect is worth recording as the reason a response
            # ends up incomplete; the application decides what to do
            # with it.

            if message.get("type") == "http.disconnect" and event is not None:
                event.data["disconnect"] = True

            return message

        async def produce() -> Any:
            if hooks.terminal is None:
                return await application(scope, receiving, sending)

            action, payload = hooks.terminal

            if action == "raises":
                raise payload() if isinstance(payload, type) else payload

            if action == "returns":
                status, headers, body = payload

                await sending(
                    {
                        "type": "http.response.start",
                        "status": _status_int(status),
                        "headers": _encoded_headers(headers),
                    }
                )

                # One http.response.body message per chunk, the last
                # flagged final, with a bare byte string wrapped as a
                # single-chunk body; an empty body still sends the one
                # message that completes the response.

                chunks = iter([body] if isinstance(body, bytes) else body)

                try:
                    pending = next(chunks)
                except StopIteration:
                    await sending(
                        {"type": "http.response.body", "body": b"", "more_body": False}
                    )
                    return None

                for chunk in chunks:
                    await sending(
                        {
                            "type": "http.response.body",
                            "body": pending,
                            "more_body": True,
                        }
                    )
                    pending = chunk

                await sending(
                    {"type": "http.response.body", "body": pending, "more_body": False}
                )
                return None

            outcome = payload(application, scope, receiving, sending)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return outcome

        # Not recording, but behaviour is configured: apply it with no
        # event anywhere.

        if event is None:
            hidden = _hide() if hidden_request else None
            silence = _silence(_SILENCE_ALL) if silenced else None
            try:
                return await produce()
            finally:
                if silence is not None:
                    _suppressed.reset(silence)
                _unhide(hidden)

        # Position before delivery, then time from after the recording
        # bookkeeping, exactly as the call wrapper does. The event
        # stays pushed for the whole coroutine, streaming included:
        # the application does all its work inside this await.

        result_policy = self._self_capture_result
        if result_policy is None and binding is not None:
            result_policy = binding._capture_result
        if result_policy is None:
            result_policy = _required_policy(active, "capture_result")

        from .bindings import _close_iteration

        token = _push(event)
        _record_event(event, active)

        started = time.perf_counter()
        event.started = started
        state["started"] = started

        def body_time() -> float:
            response_at = state["response_at"]
            if response_at is None:
                return 0.0
            last = state["last_body_at"]
            return (last - response_at) if last is not None else 0.0

        # A leaf request silences the spans beneath it; the application
        # does all its work inside this await.

        leaf_silence = _silence(_SILENCE_SPANS) if self._self_leaf else None

        try:
            outcome = await produce()
        except BaseException as exc:
            if state["response_at"] is not None and not state["complete"]:
                event.data["incomplete"] = True
            _close_iteration(
                event,
                started,
                body_time(),
                state["items"],
                result_policy,
                active,
                exception=exc,
            )
            raise
        finally:
            if leaf_silence is not None:
                _suppressed.reset(leaf_silence)
            _pop(token)

        # The coroutine returning means the response is over; a
        # response never completed on the wire is marked, which is
        # what a handled client disconnect looks like.

        if not state["complete"]:
            event.data["incomplete"] = True

        status = state["status"]
        result = _status_line(status) if status is not None else MISSING

        _close_iteration(
            event,
            started,
            body_time(),
            state["items"],
            result_policy,
            active,
            result=result,
        )

        return outcome


class RequestBehaviour:
    """The behaviour namespace for requests. ASGI mode only.

    Stages intervene while the application still runs: on the scope
    going in, on the status and headers coming out, and on each body
    chunk. Terminals replace the application. Configuration lives on
    the binding, so it persists across apply/remove cycles like every
    other behaviour namespace, and reconfiguration applies from the
    next request.
    """

    def __init__(self, binding: Binding) -> None:
        self._binding = binding
        self._hooks = binding._request_hooks

    def transforms_scope(self, fn: Callable[[Scope], Scope]) -> Binding:
        """Add a stage over the inbound scope: fn(scope) returns the
        scope the application (and later stages) will see. Mutating
        and returning the same mapping is the usual form."""

        self._hooks["inbound"].append(fn)
        return self._binding

    def transforms_response(
        self,
        fn: Callable[[Any, list[Any]], tuple[Any, list[Any]]],
    ) -> Binding:
        """Add a stage over the outbound status and headers: fn(status,
        headers) returns the pair to forward. Applied to the
        http.response.start message in its native types, an integer
        status and a list of byte-string header pairs."""

        self._hooks["response"].append(fn)
        return self._binding

    def transforms_body(self, fn: Callable[[bytes], bytes]) -> Binding:
        """Add a stage over the response body, one chunk at a time:
        fn(chunk) returns the byte string to forward in that
        http.response.body message. Hooks never see the message
        itself; its bookkeeping, more_body included, stays the
        middleware's."""

        self._hooks["body"].append(fn)
        return self._binding

    def returns(
        self,
        status: Any,
        headers: Iterable[tuple[str, str]] = (),
        body: Any = (),
    ) -> Binding:
        """Respond with a canned response; the application is not called.

        The middleware sends the response itself. status is an integer
        (ASGI carries no reason phrase), with a "200 OK" style string
        accepted by taking its leading integer; headers are (str, str)
        pairs, encoded to the byte pairs the protocol wants; and the
        body is an iterable of byte strings exactly as for WSGI, so
        pass a list even for a single chunk: [b"done"]. One
        convenience on top: a bare byte string is wrapped as a
        single-chunk body. Response and body stages still apply, and
        the recorded event is marked injected."""

        self._hooks["terminal"] = ("returns", (status, tuple(headers), body))
        self._binding._injects["request"] = True
        return self._binding

    def raises(self, exc: BaseException | type[BaseException]) -> Binding:
        """Make the request fail: the server sees the application raise.

        The application is not called, and the recorded event is marked
        injected and carries the exception."""

        self._hooks["terminal"] = ("raises", exc)
        self._binding._injects["request"] = True
        return self._binding

    def decorates(self, fn: Callable[..., Any]) -> Binding:
        """Take full custody: fn(application, scope, receive, send),
        normally an async function, drives the response itself, calling
        the application or not as it sees fit. The escape hatch, like
        on_call.decorates()."""

        self._hooks["terminal"] = ("decorates", fn)
        self._binding._injects["request"] = False
        return self._binding

    def passes_through(self) -> Binding:
        """Clear all configured request behaviour."""

        self._hooks["inbound"].clear()
        self._hooks["response"].clear()
        self._hooks["body"].clear()
        self._hooks["terminal"] = None
        self._binding._injects["request"] = False
        return self._binding
