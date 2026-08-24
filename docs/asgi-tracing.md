# ASGI request tracing

A request is the natural unit of tracing for a web application, and
an async application defeats a plain call wrapper just as a WSGI one
does, only through different plumbing. An ASGI application is a
single async callable, `app(scope, receive, send)`: the status and
headers travel through the `send` channel as an
`http.response.start` message rather than the return value, the body
is a sequence of `http.response.body` messages, and a client
disconnect surfaces as a message on the `receive` channel. Observing
a request means interposing on the two channels, which is exactly
what an ASGI middleware is.

`mode="asgi"` exists for exactly this shape. It wraps an ASGI
application object in a protocol-aware recording middleware:

```python
app = wrapture.binding("myapp.main:application", mode="asgi")
```

The mode is never detected; you always say `mode="asgi"` explicitly,
and `discover()` never selects it. Everything a binding provides
works as usual: `apply()` and `remove()`, suspend and resume,
`when=`, `capture=`, and config reachability.

For an application object that never sits at a named attribute, the
middleware is usable directly; it is the primitive the binding mode
builds on, and is itself a valid ASGI 3 application to hand to
uvicorn or hypercorn:

```python
application = wrapture.ASGIMiddleware(application)
```

The standalone constructor carries the recording options the binding
mode would otherwise supply, under the same names: `when=` decides
per request whether to record, the application running and answering
either way, as a callable taking the scope or a glob string or list
of glob strings naming request paths not to record
(`when=["/health", "/static/*"]`, matched against `scope["path"]`);
`capture_args=` is the capture policy for the request's descriptive
data, where `redact()` masks query parameters by name over and above
the built-in sensitive set; `capture_result=` the policy for the
status-line result. An explicit option wins over the bound binding's
value where both exist. The [WSGI page](wsgi-tracing.md) discusses
the same options in more detail.

Only HTTP is observed. Websocket and lifespan scopes pass through
the middleware completely untouched: they do not fit the
request-and-response shape of the `"request"` event, and would want
event kinds of their own. WSGI applications are the sibling design,
covered by [WSGI request tracing](wsgi-tracing.md).

## What a request event contains

Each request records one event of kind `"request"`, the same shape
the WSGI middleware records, so sinks, filters, assertions and
exporters need nothing new.

- `result` is the status line (`"200 OK"`), synthesised from the
  integer status the protocol carries; an unrecognised code falls
  back to the bare number (`"599"`).
- `duration` is wall time from the call to the completion of the
  application coroutine. The application drives the response from
  start to finish, so this is time to last byte by construction.
  `data["app_duration"]` is the time until the
  `http.response.start` message, how long before the response
  started; `body_duration` is the streaming tail from there to the
  final body message, and `items` counts the body messages. Because
  production and delivery are interleaved awaits inside the
  application, the tail includes time spent awaiting `send()`
  (transport backpressure), a precision limit worth knowing when
  reading the numbers.
- `data` carries the HTTP details: `interface` (`"asgi"`), `method`,
  `path` (the scope's decoded path), `query`, `scheme`, `protocol`
  (the wire protocol, `"HTTP/1.1"`, from the scope's
  `http_version`), `remote` (the client address), `content_type` and
  `content_length` from the response headers, and `bytes` counted
  from the body messages actually sent. `event.path` stays the bound
  location, so `Aggregate` remains bounded by locations, never by
  distinct URLs.
- Every binding that fires while handling the request nests beneath
  it, through the streaming tail included. Everything the
  application does happens inside its coroutine, which is awaited
  with the request event current, and concurrent requests run in
  separate server tasks with separate context copies, so parallel
  requests cannot cross-link. One caveat: a background task the
  application spawns inherits its context at creation, so events it
  records after the response completes link under a request event
  that has already closed, which is honest about where the work came
  from.
- A response never completed on the wire, the application returning
  without the final body message, closes the event with
  `data["incomplete"] = True`; when the application bailed out
  because the client went away, the disconnect message it read is
  recorded as `data["disconnect"] = True` alongside.

In the live printer a request reads access-log style, the closing
line showing the time to last byte and the body's own share of it:

```
GET /orders/42?expand=items (myapp.main.application)
  shop.OrderService.load(order_id=42)
  shop.OrderService.load -> <Order 42> [3.2ms]
myapp.main.application -> '200 OK' [11.8ms, body 4.1ms over 3 chunks]
```

The request boundary is also where distributed trace identity
arrives: a request carrying a `traceparent` header joins the
caller's trace, one with none mints a fresh identity, and either way
every event in the request's tree shares it. The
[trace identity section](ad-hoc-tracing.md#trace-identity-and-propagation)
of the ad-hoc tracing guide covers the mechanism.

## Redacting secrets from recorded requests

Query string redaction is identical to the WSGI middleware's,
because it is the same machinery: the built-in sensitive set
(`password`, `token`, `api_key`, `session_id` and the rest) is
always redacted, `redact()` names add to it with the same vocabulary
used for call arguments, redacted names pertain to query string
parameters only and never to the data fields, and a query string
that cannot be processed is recorded as the marker wholesale. See
[the WSGI page](wsgi-tracing.md#redacting-secrets-from-recorded-requests) for the full list and the
scope of what redaction can and cannot see.

```python
app = wrapture.binding("myapp.main:application", mode="asgi",
                       capture=wrapture.redact("signature"))
```

## The on_request namespace

Requests get their own behaviour namespace, shared in name with the
WSGI mode and different where the protocol is different; `on_call`
on an asgi binding raises `WrongModeError` pointing here. Stages
intervene while the application still runs:

```python
app = wrapture.binding("myapp.main:application", mode="asgi")

# Inbound: shape the scope the application sees.
def force_beta(scope):
    scope["state"] = dict(scope.get("state") or {}, feature_flags="beta")
    return scope

app.on_request.transforms_scope(force_beta)

# Outbound: rewrite status or headers on the way to the server,
# in the message's native types: an integer status and a list of
# byte-string header pairs.
def stamp(status, headers):
    return status, [*headers, (b"x-traced-by", b"wrapture")]

app.on_request.transforms_response(stamp)

# The body, one chunk at a time: fn(chunk) -> chunk, applied to each
# http.response.body message. The message bookkeeping (more_body
# included) stays the middleware's; hooks only ever see the bytes.
app.on_request.transforms_body(lambda chunk: chunk.upper())
```

Terminals replace the application, exactly as `on_call.returns()`
replaces a call:

```python
# A canned response; the application is not called, and the recorded
# event is marked injected. The status is an integer (ASGI carries
# no reason phrase; a "503 Service Unavailable" string is accepted
# by taking its leading integer), the headers are (str, str) pairs
# encoded to the byte pairs the protocol wants, and the body is an
# iterable of byte strings, a list even for a single chunk (bare
# bytes are also accepted, and served as one chunk).
app.on_request.returns(503,
                       [("Content-Type", "text/plain")],
                       [b"maintenance window\n"])

# Fault injection: the server sees the application raise.
app.on_request.raises(ConnectionResetError("backend gone"))

# Full custody: an async handler that calls the application, or not,
# as it decides.
async def short_circuit(app, scope, receive, send):
    if scope["path"] == "/health":
        await send({"type": "http.response.start",
                    "status": 204, "headers": []})
        await send({"type": "http.response.body",
                    "body": b"", "more_body": False})
        return
    await app(scope, receive, send)

app.on_request.decorates(short_circuit)
```

`passes_through()` clears all of it. Behaviour applies whether or
not anything is recording, matching every other namespace, and
`when=` receives the scope as its single positional argument:

```python
app = wrapture.binding(
    "myapp.main:application", mode="asgi",
    when=lambda _, args, kwargs: args[0]["path"].startswith("/api"),
)
```

## Asserting on requests in tests

The status is the result, so the existing assertion vocabulary needs
nothing new:

```python
def test_export_streams_and_succeeds():
    app = wrapture.binding("myapp.main:application", mode="asgi")

    with wrapture.timeline(app):
        client.get("/export.csv?limit=100")

        event = app.events.assert_once()[0]
        assert event.result == "200 OK"
        assert event.items > 1              # actually streamed
        assert event.data["bytes"] > 0
```

## From a config file

An observe entry takes `mode = "asgi"`, valid only with `name`,
never `match`: a pattern must never bulk-install middleware. For an
asgi entry, `redact` names query string parameters:

```toml
[[observe]]
target = "myapp.main"
name = "application"
mode = "asgi"
redact = ["signature"]

[[sink]]
type = "jsonlines"
path = "trace.jsonl"
```

With that config, `python -m wrapture -m uvicorn myapp.main:application`,
or the autowrapt injection path, gives request-tied tracing of an
inherited application with no code changes.

## Framework instrumentation under the covers

As on the WSGI side, the observe entry form requires knowing where
the application object lives, and framework applications often are
the application object themselves rather than delegating to a
swappable attribute. An instrumentation triggered by the framework's
own import can interpose at a construction-time choke point instead,
with `when=False` keeping the plumbing itself out of the trace; the
fastapi-app example in the repository's
[examples directory](https://github.com/GrahamDumpleton/wrapture/tree/main/examples)
is this pattern in full, pairing the middleware with `observed()` on
the route handlers.

## When the framework catches the exception

As on the WSGI side, a handler that raises rarely shows the exception
on the request event: the framework's exception middleware catches
it, hands it to an error handler that produces the 500 response, and
the request event closes with a 500 status as its result and no
exception. The failure is visible only in the handler the framework
passes the exception to, and `note_exception()` from a behaviour
bound there, aimed with `current_event(kind="request")`, carries it
to the request event the middleware recorded; an ASGI request event
is of the same kind, so the aiming is the same:

```python
def note_failure(wrapped, instance, args, kwargs):
    wrapture.current_event(kind="request").note_exception(args[-1])
    return wrapped(*args, **kwargs)
```

The request then shows its status and the exception together, on the
printed line (`-> '500 Internal Server Error' !! KeyError`), on the
tape, where `raising(KeyError)` finds it, and as an exception event
with error status on its exported span. The [WSGI
page](wsgi-tracing.md#when-the-framework-catches-the-exception) has
the full discussion and the [unit testing
guide](unit-testing.md#noting-a-caught-exception) the semantics of
the call; which method to bind is the framework's business (the
handler its exception middleware invokes, and the index of the
exception among its arguments), and a hook that binds it is the
third choke point beside construction and route registration.

## Protocol obligations, honoured

The middleware sits between server and application, so it carries an
ASGI middleware's obligations:

- Messages are forwarded unaltered and unbuffered in both
  directions, extension message types included; the recorded status
  and headers are the ones that actually went to the server.
- Non-HTTP scopes (websocket, lifespan) pass through completely
  untouched: the server sends every connection through the
  application it was given, and the middleware never gets in the
  way of protocols it does not observe.
- When nothing is recording and no behaviour is configured, the
  application is awaited with the original `receive` and `send`,
  wrapping nothing.
- The middleware is an ASGI 3 single callable and expects the same
  of the application it wraps. The legacy ASGI 2 double-callable
  form is not supported.

One limitation, stated plainly: bodies sent through server
extensions, `http.response.pathsend` (the sendfile analogue) and
zero-copy send, are forwarded and the response is still recorded as
complete, but their content is not observed: it is not counted in
`items` or `bytes`. This is the ASGI analogue of the WSGI `write()`
limitation.
