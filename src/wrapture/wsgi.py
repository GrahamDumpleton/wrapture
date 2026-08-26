"""The WSGI middleware behind mode="wsgi" bindings.

A WSGI application's calling convention defeats a plain call wrapper:
the status and headers travel through the start_response callback
rather than the return value, the body is an arbitrary iterable the
server consumes after the call returns, and errors during streaming
happen after a call event would already have closed. The middleware
here honours the protocol: it forwards start_response faithfully
(exc_info re-invocation included), never buffers or consumes the body,
relays close() to the wrapped iterable exactly once, and returns the
application's iterable untouched when nothing is recording, which also
preserves the wsgi.file_wrapper optimisation exactly when it matters.

Inside a recording scope each request records one event of kind
"request", structurally a generator event: duration is wall time from
the call to the close of the body (time to last byte), body_duration
accumulates the time spent producing chunks, and items counts them.
The status line is the event's result, and the HTTP details ride in
event.data, never in event.path, which stays the bound location.

The legacy write() callable that start_response returns is passed
through but not observed: body content sent through it is not counted
and does not extend the recorded timing.
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote_plus

from wrapt import MISSING, CallableObjectProxy

from . import trace as _trace
from .capture import NONE, CapturePolicy, _capture_value, _level_of, _resolve_policy
from .events import Event
from .sinks import (
    Sink,
    _active_sinks,
    _in_recorder,
    _notify_error,
    _record_event,
    _required_policy,
)
from .stacks import _capture as _capture_stack
from .timeline import _pop, _push, _stack, _timelines_active, seed_data

if TYPE_CHECKING:
    from .bindings import Binding

StartResponse = Callable[..., Any]
WSGIApplication = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]

# Query parameters whose values never leave the process, whatever the
# capture policy says. Matched case-insensitively against the decoded
# parameter name; names given to redact() add to this set rather than
# replace it.

_SENSITIVE_QUERY = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "client_secret",
        "session",
        "session_id",
        "sessionid",
        "sessid",
        "jsessionid",
        "phpsessid",
    }
)

_REDACTED = "<redacted>"


def _describe(app: Any) -> str:
    # A standalone middleware has no binding to name it, so derive the
    # path from the wrapped application itself. No label is derived: an
    # unnamed middleware records label None and every consumer falls
    # back to the path.

    module = getattr(app, "__module__", None) or "wsgi"
    qualname = (
        getattr(app, "__qualname__", None)
        or getattr(type(app), "__qualname__", None)
        or "application"
    )

    return f"{module}:{qualname}"


def _request_predicate(when: Any, path_of: Callable[[Any], str]) -> Any:
    """Normalize a middleware when= to the internal calling convention.

    The standalone forms are friendlier than the binding-internal
    (instance, args, kwargs) convention: a callable takes the carrier
    (the WSGI environ, or the ASGI scope) directly, and a glob string
    or an iterable of glob strings names request paths not to record,
    the form a configuration file can express. Booleans pass as with
    when= elsewhere: True is the always-record default, False never
    records. `path_of` extracts the request path from the carrier for
    the glob forms.
    """

    if when is None or when is True:
        return None

    if when is False:
        return False

    if callable(when):

        def ask(instance: Any, args: tuple[Any, ...], kwargs: Any) -> Any:
            return when(args[0])

        return ask

    if isinstance(when, str):
        when = (when,)

    try:
        patterns = tuple(when)
    except TypeError:
        patterns = None

    if patterns is None or not all(isinstance(item, str) for item in patterns):
        raise ValueError(
            f"when must be a boolean, a callable taking the request"
            f" carrier, or a glob string or iterable of glob strings"
            f" naming paths not to record, got {when!r}"
        )

    def decline(instance: Any, args: tuple[Any, ...], kwargs: Any) -> bool:
        path = path_of(args[0])
        return not any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)

    return decline


def _environ_path(environ: Any) -> str:
    # The path as recorded on the event: SCRIPT_NAME plus PATH_INFO.

    return str(environ.get("SCRIPT_NAME", "")) + str(environ.get("PATH_INFO", ""))


def _captured_query(query: str, policy: CapturePolicy) -> str:
    """The recorded form of a query string.

    Each parameter's decoded value passes through the capture policy
    under the parameter's name, so redact() covers query parameters
    with the same vocabulary it uses for call arguments; the built-in
    sensitive set is then enforced on top. The result is recorded in
    decoded display form. A query string that cannot be processed is
    recorded as the marker wholesale, never raw.
    """

    try:
        pairs: list[str] = []

        for part in query.split("&"):
            name_raw, _, value_raw = part.partition("=")
            name = unquote_plus(name_raw)
            value = unquote_plus(value_raw)

            if name.casefold() in _SENSITIVE_QUERY:
                captured: Any = _REDACTED
            else:
                captured = _capture_value(policy, name, value)

            pairs.append(f"{name}={captured}")

        return "&".join(pairs)
    except Exception:
        return _REDACTED


def _request_data(environ: Mapping[str, Any], policy: CapturePolicy) -> dict[str, Any]:
    # The environ subset recorded on the event. Values are captured at
    # the policy's level but under no name: by-name redaction pertains
    # to query string parameters only, matched inside _captured_query,
    # never to these fields. The path comes from SCRIPT_NAME and
    # PATH_INFO, never REQUEST_URI, which would smuggle the unredacted
    # query string back in.

    data: dict[str, Any] = {"interface": "wsgi"}

    path = environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "")

    plain = {
        "method": environ.get("REQUEST_METHOD", ""),
        "path": path,
        "scheme": environ.get("wsgi.url_scheme", ""),
        "protocol": environ.get("SERVER_PROTOCOL", ""),
        "remote": environ.get("REMOTE_ADDR", ""),
    }

    for name, value in plain.items():
        if value:
            data[name] = _capture_value(policy, None, value)

    query = environ.get("QUERY_STRING", "")
    if query:
        data["query"] = _captured_query(query, policy)

    return data


def _trace_environ(environ: Mapping[str, Any]) -> dict[str, str]:
    # The trace propagation headers, lifted off the environ under
    # their casefolded names.

    headers: dict[str, str] = {}

    for name in _trace.wanted_headers():
        value = environ.get("HTTP_" + name.upper().replace("-", "_"))
        if isinstance(value, str):
            headers[name] = value

    return headers


def _content_headers(event: Event, headers: Iterable[tuple[str, str]]) -> None:
    # Lift the two headers worth recording by default off the response.
    # Recorded at each start_response invocation, so an exc_info
    # replacement leaves the headers that actually went to the server.

    for name, value in headers:
        folded = name.casefold()

        if folded == "content-type":
            event.data["content_type"] = value
        elif folded == "content-length":
            try:
                event.data["content_length"] = int(value)
            except ValueError:
                event.data["content_length"] = value


class _Hooks:
    """A snapshot of a binding's request behaviour at call time.

    Shared with the ASGI middleware: the inbound stages transform the
    environ there and the scope here, but the phases are the same.
    """

    __slots__ = ("inbound", "response", "body", "terminal")

    def __init__(self, binding: Binding | None) -> None:
        hooks = binding._request_hooks if binding is not None else None

        if hooks is None:
            self.inbound: tuple[Callable[..., Any], ...] = ()
            self.response: tuple[Callable[..., Any], ...] = ()
            self.body: tuple[Callable[..., Any], ...] = ()
            self.terminal: tuple[str, Any] | None = None
        else:
            self.inbound = tuple(hooks["inbound"])
            self.response = tuple(hooks["response"])
            self.body = tuple(hooks["body"])
            self.terminal = hooks["terminal"]

    def configured(self) -> bool:
        return bool(self.inbound or self.response or self.body or self.terminal)


class _ResponseIterator:
    """The relay around a response iterable while a request records.

    A class-based iterator, never a generator: the server's close()
    call must reach a real method that closes the event and propagates
    close() to the wrapped iterable, whether the body was exhausted,
    failed, or abandoned mid-stream. The event is re-pushed around each
    __next__ only, so work the application does while streaming nests
    under the request while the server's own time between chunks does
    not, the same discipline generator recording uses.
    """

    def __init__(
        self,
        iterable: Any,
        event: Event,
        stack: tuple[Event, ...],
        started: float,
        policy: CapturePolicy,
        active: tuple[Sink, ...],
        response: dict[str, Any],
    ) -> None:
        self._iterable = iterable
        self._iterator: Any = None
        self._event = event
        self._stack = stack
        self._started = started
        self._policy = policy
        self._active = active
        self._response = response

        self._body = 0.0
        self._items = 0
        self._bytes = 0
        self._done = False
        self._closed = False

    def __iter__(self) -> _ResponseIterator:
        return self

    def _finish(
        self, result: Any = MISSING, exception: BaseException | None = None
    ) -> None:
        # Close the event exactly once, however the body ended. The
        # import is local because bindings imports this module from
        # within apply(), and a top-level import both ways would cycle
        # if this module is imported first.

        if self._done:
            return
        self._done = True

        from .bindings import _close_iteration

        _close_iteration(
            self._event,
            self._started,
            self._body,
            self._items,
            self._policy,
            self._active,
            result=result,
            exception=exception,
        )

    def __next__(self) -> Any:
        # Re-establish the request on the in-progress stack around the
        # pull, so anything the application does while producing this
        # chunk records beneath the request event.

        token = _stack.set(self._stack + (self._event,))
        resumed = time.perf_counter()

        try:
            if self._iterator is None:
                self._iterator = iter(self._iterable)
            chunk = next(self._iterator)
        except StopIteration:
            self._body += time.perf_counter() - resumed
            _stack.reset(token)
            self._finish(result=self._response.get("status", MISSING))
            raise
        except BaseException as exc:
            self._body += time.perf_counter() - resumed
            _stack.reset(token)
            self._finish(exception=exc)
            raise
        else:
            self._body += time.perf_counter() - resumed
            _stack.reset(token)

        self._items += 1
        self._event.items = self._items

        try:
            self._bytes += len(chunk)
        except TypeError:
            pass
        else:
            self._event.data["bytes"] = self._bytes

        return chunk

    def close(self) -> None:
        """Close the response: the WSGI obligation the server discharges.

        Propagates close() to the wrapped iterable exactly once, and
        closes the event if the body never ran to completion, marking
        it incomplete, which is what a client disconnect looks like. An
        exception raised by the wrapped close() is recorded and then
        re-raised, never swallowed.
        """

        if self._closed:
            return
        self._closed = True

        try:
            inner = getattr(self._iterable, "close", None)
            if inner is not None:
                inner()
        except BaseException as exc:
            self._finish(exception=exc)
            raise

        if not self._done:
            self._event.data["incomplete"] = True
            self._finish(result=self._response.get("status", MISSING))


class WSGIMiddleware(CallableObjectProxy[Any]):
    """WSGI middleware that records each request as one event.

    Wraps a WSGI application and is itself one. Usable standalone,
    wherever an application object is handed to a server:

        application = WSGIMiddleware(application)

    or installed at a named attribute by a mode="wsgi" binding, which
    adds the binding lifecycle (apply/remove, suspend, when=, config
    reachability) and the on_request behaviour namespace on top.

    When nothing is listening the application is called and its
    iterable returned untouched. When recording, the request records
    one "request" event: the status line as its result, method, path,
    query (redacted by parameter name), scheme and peer in its data,
    chunk count and body timing as iteration fields, and every binding
    that fires while handling the request nested beneath it.

    The standalone constructor carries the recording options a
    mode="wsgi" binding would otherwise supply; an explicit option
    wins over the bound binding's value.

    - `when` decides per request whether to record, behaviour still
      applying when it declines: a callable taking the environ and
      returning a boolean, or a glob string or iterable of glob
      strings naming request paths not to record ("/health",
      "/static/*"), matched against the path as recorded (SCRIPT_NAME
      plus PATH_INFO). Booleans pass as with when= elsewhere: False
      never records.

    - `capture_args` is the capture policy for the request's
      descriptive data, the query string foremost, where redact()
      masks parameters by name; `capture_result` the policy for the
      status-line result.
    """

    def __init__(
        self,
        application: WSGIApplication,
        *,
        binding: Binding | None = None,
        label: str | None = None,
        when: Any = None,
        capture_args: CapturePolicy | str | None = None,
        capture_result: CapturePolicy | str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(application)

        if binding is not None:
            path = binding._path
            label = label or binding._label
        else:
            path = _describe(application)

        self._self_binding = binding
        self._self_path = path
        self._self_label = label

        # The standalone options mirror what a mode="wsgi" binding
        # carries, for the middleware an instrumentation package builds
        # itself: an explicit option wins, else the bound binding's
        # value, else the default. when= is normalized here to the
        # internal convention, so __call__ treats both sources alike.

        self._self_when = _request_predicate(when, _environ_path)
        self._self_capture_args = _resolve_policy(capture_args)
        self._self_capture_result = _resolve_policy(capture_result)
        self._self_data = seed_data(data) or (binding._data if binding else {})

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Any:
        binding = self._self_binding
        application = self.__wrapped__

        # A suspended binding is inert: straight through, counted.

        if binding is not None and not binding._enabled():
            return application(environ, start_response)

        hooks = _Hooks(binding)

        # when=False makes this a behaviour-only binding: it never
        # records, counts nothing, and takes no part in gap detection.

        when = self._self_when
        if when is None and binding is not None:
            when = binding._when

        active = _active_sinks()
        recording = when is not False and bool(active) and not _in_recorder.get()

        if (
            when is not False
            and not recording
            and not _in_recorder.get()
            and _timelines_active()
        ):
            if binding is not None:
                binding._note_missed_call()

        # The per-request predicate decides recording only; behaviour
        # still applies when it declines, matching when= elsewhere.

        if recording and callable(when):
            guard = _in_recorder.set(True)
            try:
                wanted = when(None, (environ,), {})
            finally:
                _in_recorder.reset(guard)

            if not wanted:
                if binding is not None:
                    binding._filtered_calls += 1
                recording = False

        # The untouched fast path: nothing recording and no behaviour
        # configured means the server sees exactly what the application
        # returned, wsgi.file_wrapper included.

        if not recording and not hooks.configured():
            return application(environ, start_response)

        # Inbound stages see and may replace the environ before the
        # application (or a terminal) does.

        for stage in hooks.inbound:
            environ = stage(environ)

        response: dict[str, Any] = {}
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
                    event.data.update(_request_data(environ, args_policy))
                else:
                    event.data["interface"] = "wsgi"

                # Incoming trace context, parsed at the boundary: the
                # request joins the caller's distributed trace. With
                # no recognised headers the event's trace stays None
                # and the recording path inherits or mints as usual;
                # with the mechanism disabled (and this binding not
                # re-enabling it) nothing is parsed at all.

                if _trace._active() or (
                    binding is not None and getattr(binding, "_trace_root", False)
                ):
                    event.trace = _trace.from_headers(_trace_environ(environ))
            finally:
                _in_recorder.reset(guard)

        # Every start_response invocation, exc_info replacements
        # included, runs the response stages and refreshes what is
        # recorded, then forwards faithfully; enforcing the protocol's
        # re-invocation rules stays the server's job.

        def respond(
            status: str,
            headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Any:
            for stage in hooks.response:
                status, headers = stage(status, headers)
            headers = list(headers)

            response["status"] = status
            if event is not None:
                _content_headers(event, headers)

            if exc_info is None:
                return start_response(status, headers)
            return start_response(status, headers, exc_info)

        def produce() -> Any:
            if hooks.terminal is None:
                return application(environ, respond)

            action, payload = hooks.terminal

            if action == "raises":
                raise payload() if isinstance(payload, type) else payload

            if action == "returns":
                status, headers, body = payload
                respond(status, list(headers))
                return [body] if isinstance(body, bytes) else body

            return payload(application, environ, respond)

        # Not recording, but behaviour is configured: apply it with no
        # event anywhere.

        if event is None:
            iterable = produce()

            for stage in hooks.body:
                iterable = stage(iterable)
            return iterable

        # Position before delivery, then time from after the recording
        # bookkeeping, exactly as the call wrapper does. The scope here
        # covers the synchronous phase; the body records around each
        # chunk instead.

        base = _stack.get()
        token = _push(event)
        _record_event(event, active)

        started = time.perf_counter()
        event.started = started

        try:
            iterable = produce()

            for stage in hooks.body:
                iterable = stage(iterable)
        except BaseException as exc:
            event.duration = time.perf_counter() - started
            event.exception = exc
            _notify_error(event, active)
            raise
        finally:
            _pop(token)

        event.data["app_duration"] = time.perf_counter() - started

        result_policy = self._self_capture_result
        if result_policy is None and binding is not None:
            result_policy = binding._capture_result
        if result_policy is None:
            result_policy = _required_policy(active, "capture_result")

        return _ResponseIterator(
            iterable, event, base, started, result_policy, active, response
        )


class RequestBehaviour:
    """The behaviour namespace for requests. WSGI mode only.

    Stages intervene while the application still runs: on the environ
    going in, on the status and headers coming out, and on the body
    iterable. Terminals replace the application. Configuration lives on
    the binding, so it persists across apply/remove cycles like every
    other behaviour namespace, and reconfiguration applies from the
    next request.
    """

    def __init__(self, binding: Binding) -> None:
        self._binding = binding
        self._hooks = binding._request_hooks

    def transforms_environ(
        self, fn: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> Binding:
        """Add a stage over the inbound environ: fn(environ) returns the
        environ the application (and later stages) will see. Mutating
        and returning the same mapping is the usual form."""

        self._hooks["inbound"].append(fn)
        return self._binding

    def transforms_response(
        self,
        fn: Callable[[str, list[tuple[str, str]]], tuple[str, list[tuple[str, str]]]],
    ) -> Binding:
        """Add a stage over the outbound status and headers: fn(status,
        headers) returns the pair to forward. Runs on every
        start_response invocation, exc_info replacements included, so
        the recorded response and the served response cannot diverge."""

        self._hooks["response"].append(fn)
        return self._binding

    def transforms_body(self, fn: Callable[[Any], Any]) -> Binding:
        """Add a stage over the response iterable: fn(iterable) returns
        the iterable the server will consume. An IteratorProxy from
        iterator() drops in directly for streaming bodies; note it
        refuses non-iterator iterables such as a list body."""

        self._hooks["body"].append(fn)
        return self._binding

    def returns(
        self,
        status: str,
        headers: Iterable[tuple[str, str]] = (),
        body: Any = (),
    ) -> Binding:
        """Respond with a canned response; the application is not called.

        The middleware calls start_response itself with the given
        status and headers, and the body is served. As with a WSGI
        application's return, the body is an iterable of byte strings,
        so pass a list even for a single chunk: [b"done"]. One
        convenience on top: a bare byte string is wrapped as a
        single-chunk body rather than being iterated element by
        element. Response and body stages still apply, and the
        recorded event is marked injected."""

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
        """Take full custody: fn(application, environ, start_response)
        produces the response iterable, calling the application or not
        as it sees fit. The escape hatch, like on_call.decorates()."""

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
