# WSGI request tracing

A request is the natural unit of tracing for a web application: one
HTTP request, its method, path and status, and every observed call
made while handling it, as one tree. A plain binding on the
application callable cannot deliver that, because WSGI routes the
interesting facts around the return value: the status and headers
travel through the `start_response` callback, the body is an iterable
the server consumes after the call returns, and a streaming
application does most of its work after a call event would already
have closed.

`mode="wsgi"` exists for exactly this shape. It wraps a WSGI
application object in a protocol-aware recording middleware:

```python
app = wrapture.binding("myapp.wsgi:application", mode="wsgi")
```

The mode is never detected; a WSGI application looks like any other
callable, so you always say `mode="wsgi"` explicitly, and `discover()`
never selects it. Everything a binding provides works as usual:
`apply()` and `remove()`, suspend and resume, `when=`, `capture=`, and
config reachability.

For an application object that never sits at a named attribute, a
closure handed straight to a server, the middleware is usable
directly; it is the primitive the binding mode builds on:

```python
application = wrapture.WSGIMiddleware(application)
```

The standalone constructor carries the recording options the binding
mode would otherwise supply, under the same names, so a middleware
built in code loses nothing to the config-declared form. `when=`
decides per request whether to record, the application running and
answering either way: a callable taking the environ, or a
`filter_requests()` filter over the request's recorded fields
(`when=wrapture.filter_requests(ignore={"path": ["/health",
"/static/*"]})`), the form that keeps health checks and static
assets out of the tape; `tree=True` extends a decline to everything
beneath the request, which is what an ignored request wants (see
[Ignoring whole requests](#ignoring-whole-requests)). `capture_args=`
is the capture policy for the
request's descriptive data, where `redact("voucher")` masks query
parameters by name over and above the built-in sensitive set,
`capture_result=` the policy for the status-line result, and `data=`
a mapping of static tags every request event starts with (the
request's own fields are written over it). An explicit option wins
over the bound binding's value where both exist.

## What a request event contains

Each request records one event of kind `"request"`. It is structurally
a generator event: the synchronous application call is one phase, then
the server drives a streaming tail as it iterates the body.

- `result` is the status line (`"200 OK"`), the outcome of a request
  the way a return value is the outcome of a call, so every existing
  filter, assertion, sink and exporter shows it unchanged.
- `duration` is wall time from the call to the close of the body:
  time to last byte. `body_duration` accumulates the time spent
  producing body chunks, and `items` counts the chunks.
- `data` carries the HTTP details: `interface` (`"wsgi"`, the gateway
  interface the request came through), `method`, `path`
  (`SCRIPT_NAME` plus `PATH_INFO`), `query`, `scheme` (from
  `wsgi.url_scheme`), `protocol` (`SERVER_PROTOCOL`, the wire
  protocol such as `"HTTP/1.1"`), `remote`, `content_type` and
  `content_length` from the response headers, `bytes` counted from
  the chunks actually served, and `app_duration`, the synchronous
  phase alone. `event.path` stays the bound location, so `Aggregate`
  remains bounded by locations, never by distinct URLs.
- Every binding that fires while handling the request nests beneath
  it, through the synchronous phase and the streaming tail alike: a
  call made between two yields of the body parents under the request.
- A response the server closes before exhaustion, a client
  disconnect, closes the event with `data["incomplete"] = True`. A
  response the server never closes leaves the event visibly open, the
  same signal an abandoned generator gives.

In the live printer a request reads access-log style, with the
closing line arriving when the body closes and showing the time to
last byte alongside the body's own share:

```
GET /orders/42?expand=items (myapp.wsgi.application)
  shop.OrderService.load(order_id=42)
  shop.OrderService.load -> <Order 42> [3.2ms]
myapp.wsgi.application -> '200 OK' [11.8ms, body 4.1ms over 3 chunks]
```

The request boundary is also where distributed trace identity
arrives: a request carrying a `traceparent` header joins the
caller's trace, one with none mints a fresh identity, and either way
every event in the request's tree shares it. The
[trace identity section](ad-hoc-tracing.md#trace-identity-and-propagation)
of the ad-hoc tracing guide covers the mechanism.

## Redacting secrets from recorded requests

Query strings carry secrets, so redaction is on by default and cannot
be switched off: the values of `password`, `passwd`, `secret`,
`token`, `access_token`, `refresh_token`, `id_token`, `api_key`,
`apikey`, `client_secret`, `session`, `session_id`, `sessionid`,
`sessid`, `jsessionid`, `phpsessid`, `sig`, `signature`,
`X-Amz-Signature` and `X-Goog-Signature`, matched
case-insensitively, are replaced with `<redacted>` before the query
is recorded anywhere.

Beyond the built-in set, the query passes through the binding's
capture policy parameter by parameter, so `redact()` names query
parameters with the same vocabulary it uses for call arguments, and
names it is given add to the default set rather than replace it:

```python
app = wrapture.binding("myapp.wsgi:application", mode="wsgi",
                       capture=wrapture.redact("signature"))
```

Redacted names pertain to query string parameters only: they never
match the recorded data fields, so `redact("path")` blanks a
parameter named `path` while the recorded URL path stays intact.

A query string that cannot be parsed is recorded as the marker
wholesale, never raw. The recorded `path` is built from `SCRIPT_NAME`
and `PATH_INFO`, never `REQUEST_URI`, so the query cannot leak back
in through the other field. This covers what is *recorded*: `when=`
predicates and `transforms_environ` stages are code intervening in
the request and see the real environ, and a secret somewhere by-name
redaction cannot see, in a path segment or the request body, is out
of its scope.

## Ignoring whole requests

Health checks, readiness probes and static assets make up most of
the traffic on many services and none of the interest. `when=` on a
request binding decides per request whether to record, and
`filter_requests()` is that decision declared as tables of request
fields to glob patterns rather than written as a callable:

```python
app = wrapture.binding(
    "myapp.wsgi:application", mode="wsgi",
    when=wrapture.filter_requests(ignore={"path": ["/health", "/static/*"]}),
    tree=True,
)
```

The fields are the ones the request event records: `method`, `path`,
`scheme`, `protocol` and `remote`, valued exactly as the event would
record them (the path is `SCRIPT_NAME` plus `PATH_INFO`, never
`REQUEST_URI`). Each field maps to one `fnmatchcase` glob or a list
of them, any of which may match, so a list of plain strings reads as
a set of exact values; methods compare case-insensitively. `ignore`
names requests not to record: a request matching any pattern of any
field it lists is declined. `accept` names the requests to record:
every field it lists must match. Given both, a request must pass
both, so `ignore` wins where they overlap, and a field absent from a
table is unconstrained:

```python
wrapture.filter_requests(
    accept={"method": ["GET", "POST", "DELETE"], "scheme": "https"},
    ignore={"path": ["/health", "/static/*"], "remote": "10.0.0.*"},
)
```

An empty table, a table naming something that is not a request
field, and a call with neither table are errors at construction, not
filters that can never act. The filter decides recording only: a
declined request is served exactly as it would be otherwise, and the
decline counts on the binding's `filtered_calls`.

`tree=True` is the other half. On its own, `when=` declines one
event, the request's, and whatever records while the request is
served (the view, its queries, a template render) still records,
each as a root of its own with no request above it: a declined
health check would leave its `SELECT 1` on the tape as an anonymous
tree. `tree=True` extends the decline to everything beneath the
request for its whole extent, streaming body included, so an ignored
request is gone entirely; the inner bindings count the skip on their
own `filtered_calls`, so a shorter tape stays explainable. It is a
flag on `when=` rather than a mode of the filter, because the two
reaches are both wanted: a behaviour-only binding (`when=False`) on
an interception point must not silence what runs beneath it, while
an ignored request must. `tree=True` without a `when=` to act on is
refused.

The request modes are where `filter_requests()` applies; a call or
attribute binding refuses it, since the fields it names exist only on
a request, and takes a callable `when=` instead. `tree=True` applies
to every binding kind: the [ad-hoc tracing
guide](ad-hoc-tracing.md#declining-a-whole-tree-tree) covers it on
calls.

## The on_request namespace

Requests get their own behaviour namespace, named for the event kind
as `on_call` is; `on_call` on a wsgi binding raises `WrongModeError`
pointing here. Stages intervene while the application still runs:

```python
app = wrapture.binding("myapp.wsgi:application", mode="wsgi")

# Inbound: shape what the application sees.
def force_beta(environ):
    environ["HTTP_X_FEATURE_FLAGS"] = "beta-checkout"
    return environ

app.on_request.transforms_environ(force_beta)

# Outbound: rewrite status or headers on the way to the server. Runs
# on every start_response invocation, exc_info replacements included.
def stamp(status, headers):
    return status, [*headers, ("X-Traced-By", "wrapture")]

app.on_request.transforms_response(stamp)

# The body: fn(iterable) -> iterable. An iterator() proxy drops in
# directly for streaming bodies.
app.on_request.transforms_body(
    wrapture.iterator().on_item.transforms_item(lambda chunk: chunk.upper())
)
```

Terminals replace the application, exactly as `on_call.returns()`
replaces a call:

```python
# A canned response; the application is not called, and the recorded
# event is marked injected. As in WSGI itself, the body is an
# iterable of byte strings, a list even for a single chunk (bare
# bytes are also accepted, and served as one chunk).
app.on_request.returns("503 Service Unavailable",
                       [("Content-Type", "text/plain")],
                       [b"maintenance window\n"])

# Fault injection: the server sees the application raise.
app.on_request.raises(ConnectionResetError("backend gone"))

# Full custody: call the application, or not, as the handler decides.
def short_circuit(app, environ, start_response):
    if environ["PATH_INFO"] == "/health":
        start_response("204 No Content", [])
        return []
    return app(environ, start_response)

app.on_request.decorates(short_circuit)
```

`passes_through()` clears all of it. Behaviour applies whether or not
anything is recording, matching every other namespace, and `when=`
receives the environ as its single positional argument, so a
predicate can record only what matters:

```python
app = wrapture.binding(
    "myapp.wsgi:application", mode="wsgi",
    when=lambda _, args, kwargs: args[0]["PATH_INFO"].startswith("/api"),
)
```

A `filter_requests()` filter is accepted in the callable's place, and
`tree=True` extends a decline to everything beneath the request; see
[Ignoring whole requests](#ignoring-whole-requests).

## Asserting on requests in tests

The status is the result, so the existing assertion vocabulary needs
nothing new:

```python
def test_export_streams_and_succeeds():
    app = wrapture.binding("myapp.wsgi:application", mode="wsgi")

    with wrapture.timeline(app):
        client.get("/export.csv?limit=100")

        event = app.events.assert_once()[0]
        assert event.result == "200 OK"
        assert event.items > 1              # actually streamed
        assert event.data["bytes"] > 0
```

## From a config file

An observe entry takes `mode = "wsgi"`, valid only with `name`, never
`match`: a pattern must never bulk-install middleware. For a wsgi
entry, `redact` names query string parameters, a `data` table seeds
every request event with static tags (the request's own fields are
written over it, so it cannot respell them), and a `requests` table
is `filter_requests()` spelt as TOML, with `tree=True` implied, since
a request a config ignores is meant to be gone entirely:

```toml
[[observe]]
target = "myapp.wsgi"
name = "application"
mode = "wsgi"
redact = ["signature"]
requests = { ignore = { path = ["/health", "/static/*"] } }

[[sink]]
type = "jsonlines"
path = "trace.jsonl"
```

The long form suits a filter with several fields a side; a sub-table
attaches to the `[[observe]]` entry above it:

```toml
[[observe]]
target = "myapp.wsgi"
name = "application"
mode = "wsgi"

[observe.requests]
ignore.path = ["/health", "/static/*"]
accept.method = ["GET", "POST", "DELETE"]
```

The keys are `accept` and `ignore`, each a table of request field to
one glob or a list of globs, with the meanings and rules of
[Ignoring whole requests](#ignoring-whole-requests); `requests` on an
entry without `mode` is a config error, as is a field that is not a
request field.

With that config, `python -m wrapture manage.py runserver`, or the
autowrapt injection path, gives request-tied tracing of an inherited
application with no code changes: the observe entry defers like any
other, the middleware lands when the application module is imported,
and every request appears in the trace as one tree.

## Framework instrumentation under the covers

The observe entry form requires knowing where the application object
lives. For the out-of-box experience, where a config should not have
to name the app at all, and for application-factory patterns where
no importable attribute ever holds it, the middleware is also usable
directly, and an instrumentation triggered by the framework's own
import can install it on every instance at construction:

```python
class FlaskInstrumentation(wrapture.Instrumentation):
    target = "flask"
    removable = True

    @wrapture.instrumentation_hook("flask")
    def flask(self, name, module):
        def wrap_app(wrapped, instance, args, kwargs):
            outcome = wrapped(*args, **kwargs)
            instance.wsgi_app = wrapture.WSGIMiddleware(
                instance.wsgi_app, label=f"{instance.name}.wsgi_app"
            )
            return outcome

        constructor = wrapture.binding(module.Flask, "__init__", when=False)
        constructor.on_call.decorates(wrap_app).apply()

        self.on_cleanup(constructor.remove)
```

The `when=False` makes this a behaviour-only binding: it installs the
middleware but never records its own calls, so the instrumentation
plumbing (here, every `Flask()` construction) stays out of the trace
it exists to produce.

Pair it with a binding on the framework's route-registration choke
point substituting `observed()` on each view function, and one
`[[instrument]]` entry instruments the whole framework; shipped as an
installed package with an entry point, the config reduces to
`[[instrument]]` with `name = "flask"`. The flask-app example in the
repository's
[examples directory](https://github.com/GrahamDumpleton/wrapture/tree/main/examples)
is this pattern in full.

## When the framework catches the exception

A request whose view raises rarely shows the exception on the request
event. The framework catches it around the dispatch, hands it to an
error handler (`Flask.handle_exception`, Django's
`handle_uncaught_exception`), and the handler returns the 500
response, so the middleware sees the application return normally:
the request event closes with `'500 INTERNAL SERVER ERROR'` as its
result and no exception, and only the view's own event, if the view
is observed, shows the `KeyError` escaping. The request did fail,
and the one place the failure can be seen is the handler, where the
exception arrives as an argument.

`note_exception()` is the call for that place, and the error handler
is the third choke point a framework hook binds:

```python
def note_failure(wrapped, instance, args, kwargs):
    wrapture.current_event(kind="request").note_exception(args[0])
    return wrapped(*args, **kwargs)

wrapture.binding(module.Flask, "handle_exception", when=False) \
    .on_call.decorates(note_failure).apply()
```

`current_event(kind="request")` aims the note past the handler's own
call at the nearest enclosing request event, the one the middleware
recorded, which no binding of the hook's own created; with nothing
recording, or the handler invoked outside a request, the handle is
empty and the note quietly does nothing. The request then says both things at once, on the printed
line, on the tape and on an exported span: it answered 500, and the
`KeyError` was why.

```text
GET /quote/missing (myapp.wsgi_app)
  myapp.quoted(item='missing')
  myapp.quoted !! KeyError [78us]
myapp.wsgi_app -> '500 INTERNAL SERVER ERROR' !! KeyError [2.7ms, body 11us over 1 chunk]
```

The view's event and the request's both carry the same `KeyError`,
one as the escape and one as the note, because two scopes failed for
the same reason; `tape.for_binding(...).raising(KeyError)` finds
either. The [unit testing
guide](unit-testing.md#noting-a-caught-exception) has the full
semantics of the call, and the flask-app example binds
`handle_exception` exactly this way.

## Protocol obligations, honoured

The middleware sits between server and application, so it carries the
middleware obligations of PEP 3333:

- `start_response` is forwarded faithfully on every invocation,
  `exc_info` re-invocation included; the recorded status is the last
  one forwarded, so a 200-then-500 replacement records the 500.
  Enforcing the re-invocation rules remains the server's job.
- The response iterable is relayed, never buffered or consumed, and
  the server's `close()` call is propagated to the wrapped iterable
  exactly once, on normal exhaustion, failure and abandonment alike.
  An exception raised by the wrapped `close()` is recorded on the
  event and re-raised, never swallowed.
- When nothing is recording and no behaviour is configured, the
  application's iterable is returned untouched, which preserves the
  `wsgi.file_wrapper` optimisation exactly when it matters. While a
  request records, the body is necessarily wrapped, so sendfile is
  bypassed for that request; that is the cost of observing it. A
  request a `tree=True` filter declined is served with recording
  suppressed beneath it: a lazily produced body is wrapped so that
  streaming stays silenced, while a list body and the server's own
  `wsgi.file_wrapper` are returned untouched, so sendfile survives
  for the static assets a filter typically ignores.

One limitation, stated plainly: the legacy `write()` callable that
`start_response` returns is passed through but not observed, so body
content sent through it is not counted and does not extend the
recorded timing (PEP 3333 keeps `write()` only for pre-WSGI
backwards compatibility). ASGI applications are the sibling design,
covered by [ASGI request tracing](asgi-tracing.md).
