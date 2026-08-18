# Finding where a request spends its time

One endpoint is slow. The handler calls a service, the service calls a
repository, and a template renders the result, and the question is
which of those layers the time is going to. A profiler answers with
every frame in the process, most of them framework internals you do
not care about, and it cannot tell one request from the next. Logging
answers only where someone already thought to add a timer.

wrapture lets you name the four layers you suspect, record one request
as one tree, and read the time off each level, in a live interpreter,
in a test, or against a running application from a config file, with
none of the application's code changed.

This example uses: [WSGI request
tracing](wsgi-tracing.md#what-a-request-event-contains), [observing a
bare callable](unit-testing.md#observing-a-bare-callable), the
[Printer sink](ad-hoc-tracing.md#watching-calls-live-printer),
[sink combinators](ad-hoc-tracing.md#composing-sinks-fan-out-sampling-and-filtering),
[the call tree](unit-testing.md#the-call-tree-and-ordering),
[annotation](unit-testing.md#recording-calls-on-a-timeline),
[exporters](ad-hoc-tracing.md#exporting-traces-to-other-tools) and
[configuring from a file](ad-hoc-tracing.md#configuring-from-a-file).

## The application

A stand-in for the web application: a repository (deliberately slow),
a service over it, a template, a view function, and a WSGI application
object that dispatches to views by name and exposes its callable as
`wsgi_app`, the way Flask does. Nothing here imports wrapture.

```python
>>> import sys
>>> import time
>>> import wrapture

>>> CATALOG = {"widget": 25, "gadget": 120}

>>> class Repository:
...     def fetch(self, item: str) -> int:
...         time.sleep(0.005)
...         return CATALOG[item]

>>> class QuoteService:
...     def __init__(self, repository: Repository) -> None:
...         self.repository = repository
...     def quote(self, item: str) -> dict:
...         price = self.repository.fetch(item)
...         return {"item": item, "price": price}

>>> class Template:
...     def render(self, quote: dict) -> bytes:
...         return f"{quote['item']}: {quote['price']}\n".encode()

>>> service = QuoteService(Repository())
>>> template = Template()

>>> def quote_view(item: str) -> bytes:
...     return template.render(service.quote(item))

>>> class Shop:
...     def __init__(self) -> None:
...         self.routes = {"quote": quote_view}
...     def wsgi_app(self, environ, start_response):
...         _, name, item = environ["PATH_INFO"].split("/", 2)
...         body = self.routes[name](item)
...         start_response("200 OK", [("Content-Type", "text/plain")])
...         return [body]
...     def __call__(self, environ, start_response):
...         return self.wsgi_app(environ, start_response)

>>> shop = Shop()

```

To drive it without a server, a helper plays the server's part: build
an environ, call the application, consume the body, then close it. The
close matters, because a request event ends when its body closes.

```python
>>> from wsgiref.util import setup_testing_defaults

>>> def get(app, path: str, **headers: str) -> tuple[str, bytes]:
...     environ = {"PATH_INFO": path, **headers}
...     setup_testing_defaults(environ)
...     status = []
...     body = app(environ, lambda s, h: status.append(s))
...     chunks = b"".join(body)
...     close = getattr(body, "close", None)
...     if close is not None:
...         close()
...     return status[0], chunks

>>> get(shop, "/quote/widget")
('200 OK', b'widget: 25\n')

```

## The naive approach

The usual move is a stopwatch around each suspect: `time.perf_counter()`
before and after the service call, a log line with the difference,
another pair around the repository, and so on down. Each timer is a
code change in a layer that should not know it is being measured, the
numbers arrive as separate log lines that you correlate by eye, and
none of them are tied to the request they belong to, so a slow request
among fast ones is invisible in the average.

## Step 1: one request as one event

`WSGIMiddleware` wraps a WSGI application and records each request as
a single event: the method and path on the opening line, the status
line as the result, and the wall time from the call to the close of
the body as the duration. Inside a `timeline()` the request lands on
the tape:

```python
>>> application = wrapture.WSGIMiddleware(shop, label="shop")

>>> with wrapture.timeline() as tape:
...     _ = get(application, "/quote/widget")
...     print(tape.tree())
GET /quote/widget (shop)  -> '200 OK'

>>> request = tape.all[0]
>>> request.kind, request.result, request.items
('request', '200 OK', 1)
>>> sorted(request.data)
['app_duration', 'bytes', 'content_type', 'interface', 'method', 'path', 'protocol', 'scheme']

```

`request.duration` is time to last byte; `data["app_duration"]` is the
synchronous phase alone, the call that returned the iterable, and
`body_duration` is the time spent producing chunks. For this
application the body is one chunk built before the call returned, so
almost all of the time is in the synchronous phase, which is where the
layers beneath run.

## Step 2: the layers beneath the request

The service, repository and template are methods on classes, so
bindings reach them, held here in one group and applied together. The
view function lives in a dispatch table rather than at an attribute,
so `observed()` wraps the value instead, and the wrapped value goes
where the original was. Once applied, every call through them nests
beneath whichever request is in flight:

```python
>>> layers = wrapture.bindings(
...     quote=(QuoteService, "quote"),
...     fetch=(Repository, "fetch"),
...     render=(Template, "render"),
... )
>>> _ = layers.apply()
>>> shop.routes["quote"] = wrapture.observed(quote_view)

>>> with wrapture.timeline() as tape:
...     _ = get(application, "/quote/widget")
...     print(tape.tree())
GET /quote/widget (shop)  -> '200 OK'
  __main__.quote_view(item='widget')  -> b'widget: 25\n'
    quote(item='widget')  -> {'item': 'widget', 'price': 25}
      fetch(item='widget')  -> 25
    render(quote={'item': 'widget', 'price': 25})  -> b'widget: 25\n'

```

That is the request as it ran, with real arguments and real results,
and no timer in any of the layers.

## Step 3: watching it live, with timings

`Printer` is a process sink: register it and every recorded event
prints as it happens, an opening line as each operation begins and a
closing line with the outcome and the elapsed time. No timeline is
needed; the applied bindings and the sink are enough. Timing is on by
default:

```python
>>> printer = wrapture.add_sink(wrapture.Printer(sys.stdout))

>>> _ = get(application, "/quote/gadget")
GET /quote/gadget (shop)
  __main__.quote_view(item='gadget')
    quote(item='gadget')
      fetch(item='gadget')
      fetch -> 120 [...ms]
    quote -> {'item': 'gadget', 'price': 120} [...ms]
    render(quote={'item': 'gadget', 'price': 120})
    render -> b'gadget: 120\n' [...us]
  __main__.quote_view -> b'gadget: 120\n' [...ms]
shop -> '200 OK' [...ms, body ...us over 1 chunk]

>>> wrapture.remove_sink(printer)

```

The answer is already readable: `fetch` accounts for essentially all of
`quote`, which accounts for essentially all of the view, and `render`
is microseconds. The request's closing line shows time to last byte
and the body's own share.

## Step 4: printing less

On a busy application the full tree per request is too much. The
combinators wrap a sink and gate what reaches it. `Depth(1, ...)`
forwards only the roots, which for a web application means one
opening and one closing line per request, an access log with timings:

```python
>>> printer = wrapture.add_sink(wrapture.Depth(1, wrapture.Printer(sys.stdout)))

>>> _ = get(application, "/quote/gadget")
GET /quote/gadget (shop)
shop -> '200 OK' [...ms, body ...us over 1 chunk]

>>> wrapture.remove_sink(printer)

```

`Filter(predicate, ...)` forwards only events the predicate accepts,
decided once as each event enters, so it selects by what the event is,
its kind, path or label, not by how long it turns out to take. Here it
keeps the request lines and the repository, the layer under suspicion,
and drops the rest:

```python
>>> suspects = wrapture.Filter(
...     lambda event: event.kind == "request" or event.path.endswith("fetch"),
...     wrapture.Printer(sys.stdout, timing=False),
... )
>>> printer = wrapture.add_sink(suspects)

>>> _ = get(application, "/quote/gadget")
GET /quote/gadget (shop)
      fetch(item='gadget')
      fetch -> 120
shop -> '200 OK'

>>> wrapture.remove_sink(printer)

```

`Sample(rate, ...)` keeps a random fraction of whole request trees, the
usual choice when the printer must stay on under real traffic. Because
a duration is known only when an event closes, "only the slow
requests" is a few lines of your own sink rather than a filter: hear
`on_exit`, and print when the request took longer than a threshold.

```python
>>> class SlowRequests(wrapture.Sink):
...     def __init__(self, threshold: float, stream) -> None:
...         self.threshold = threshold
...         self.stream = stream
...     def on_exit(self, event) -> None:
...         if event.kind == "request" and event.duration > self.threshold:
...             where = f"{event.data.get('method')} {event.data.get('path')}"
...             print(f"slow: {where} {event.duration * 1000:.1f}ms", file=self.stream)

>>> slow = wrapture.add_sink(SlowRequests(0.003, sys.stdout))
>>> _ = get(application, "/quote/gadget")
slow: GET /quote/gadget ...ms
>>> wrapture.remove_sink(slow)

```

## Step 5: reading the time off the tree

The tape gives the same picture after the fact, and adds the figure
that settles the question: self time, an event's execution time minus
that of its observed children. `tape.tree(times=True)` shows both, and
`tape.self_time(event)` gives it for one event:

```python
>>> with wrapture.timeline() as tape:
...     _ = get(application, "/quote/widget")
...     print(tape.tree(times=True))
GET /quote/widget (shop)  -> '200 OK'  [...ms, self ...us]
  __main__.quote_view(item='widget')  -> b'widget: 25\n'  [...ms, self ...us]
    quote(item='widget')  -> {'item': 'widget', 'price': 25}  [...ms, self ...us]
      fetch(item='widget')  -> 25  [...ms]
    render(quote={'item': 'widget', 'price': 25})  -> b'widget: 25\n'  [...us]

>>> request, view, quote, fetch, render = tape.all
>>> tape.self_time(fetch) > 0.9 * view.duration
True
>>> tape.self_time(quote) < 0.1 * quote.duration
True

```

`quote` and the view are slow only because of what they call; `fetch`
is slow in its own right. That distinction, "slow itself" versus "slow
because of a child", is what the self time column carries and what a
wall-clock timer around the service call cannot express.

One thing to know about the request row: a request streams, so its
figure in the tree is the application's own time, the synchronous
phase plus the time spent producing body chunks, leaving out any time
the server took between chunks. The whole-request wall time and the
two phases are on the event itself, and the view's own duration sits
inside the synchronous phase:

```python
>>> request.body_duration < request.data["app_duration"] <= request.duration
True
>>> view.duration <= request.data["app_duration"]
True

```

## Step 6: tagging a request with who it was for

A slow endpoint is often slow for one tenant, one account or one
request id, and the middleware cannot know which header carries that.
`annotate(**data)` merges values into the in-flight event's `data`, and
a `decorates()` handler on the request is a place to call it from where
the environ is in hand. That handler lives on the `on_request`
namespace of a `mode="wsgi"` binding, which installs the same
middleware at the attribute the application exposes, so the direct
`WSGIMiddleware` from step 1 is set aside here in favour of the binding
form:

```python
>>> app = wrapture.binding(shop, "wsgi_app", mode="wsgi", label="shop")

>>> def tag(application, environ, start_response):
...     wrapture.annotate(tenant=environ.get("HTTP_X_TENANT"),
...                       request_id=environ.get("HTTP_X_REQUEST_ID"))
...     return application(environ, start_response)

>>> _ = app.on_request.decorates(tag)

>>> with wrapture.timeline(app) as tape:
...     _ = get(shop, "/quote/widget", HTTP_X_TENANT="acme", HTTP_X_REQUEST_ID="r-1")
...     _ = get(shop, "/quote/gadget", HTTP_X_TENANT="globex", HTTP_X_REQUEST_ID="r-2")
...     globex = app.events.matching(lambda e: e.data.get("tenant") == "globex")
...     print(globex)
...     [child.label for child in tape.children_of(globex[0])]
<EventLog shop[matching=<lambda>]: 1 event(s)>
    GET /quote/gadget (shop)
['__main__.quote_view']

```

The tags ride on the request event, so a `Filter` predicate reading
`event.data.get("tenant")` narrows a printer to one tenant's requests,
and in a test the same expression selects the request to assert on;
`tape.children_of()` walks from there to the layers beneath it.
`shop(...)` is called directly here rather than through `application`,
because the binding has patched `shop.wsgi_app` and wrapping the app
twice would record every request twice.

## Step 7: handing the trace to other tools

For a request with a dozen layers the printed tree stops being the
easiest reading. `chrome_trace()` renders a tape as Chrome trace JSON,
which the [Perfetto UI](https://ui.perfetto.dev) opens as a timeline
with nested slices whose widths are the durations, and `mermaid()`
renders it as a sequence diagram that pastes into a pull request:

```python
>>> import json
>>> trace = json.loads(wrapture.chrome_trace(tape))
>>> len([e for e in trace["traceEvents"] if e["ph"] == "X"])
10

>>> print(wrapture.mermaid(tape))
sequenceDiagram
    participant caller
    participant P1 as Shop
    participant P2 as __main__
    participant P3 as QuoteService
    participant P4 as Repository
    participant P5 as Template
    caller->>+P1: GET /quote/widget
    P1->>+P2: quote_view
    P2->>+P3: quote
    P3->>+P4: fetch
    P4-->>-P3: return
    P3-->>-P2: return
    P2->>+P5: render
    P5-->>-P2: return
    P2-->>-P1: return
    P1-->>-caller: return
    caller->>+P1: GET /quote/gadget
    ...

```

Both exporters also accept the records a `JSONLines` sink wrote, so a
trace file from a running server renders the same way, from a shell as
`python -m wrapture.tools convert --format chrome -o trace.json trace.jsonl`.

That is the end of the in-process walk, so take the patches down:

```python
>>> _ = layers.remove()
>>> shop.routes["quote"] = quote_view

```

## The same thing from a config file

Against a real application, none of the above needs to be code. The
`flask-app` directory in the repository's
[examples](https://github.com/GrahamDumpleton/wrapture/tree/main/examples)
is a small Flask shop whose `wrapture.toml` installs the WSGI
middleware on every Flask instance and observes every view function
from a `[[setup]]` hook, and names the application's own `quote` helper
in an `[[observe]]` entry. To get the timing view of this page, the
`[[sink]]` entry is a printer with `timing` on (the default, shown here
for emphasis), and `depth` gates it to the request, the view and one
layer beneath:

```toml
pythonpath = "."

[[observe]]
target = "myapp"
name = "quote"

[[setup]]
module = "flask"
call = "wrapture_local.flask_support:instrument"

[[sink]]
type = "printer"
timing = true
timestamps = true
depth = 3
```

Every key after `type` goes to the `Printer` constructor, except the
gating keys `depth`, `sample` and `filter`, which are the file's
spelling of the combinators from step 4; `filter = { kind = "request" }`
would give the access-log view. Save it as `timing.toml` next to the
example and run the example's script under it; the runner applies the
config, then runs `main.py` as `__main__` (Flask is not a dependency
of the repository, so uv supplies it):

```console
$ cd examples/flask-app
$ uv run --with flask python -m wrapture --config timing.toml main.py
18:26:03.144 GET / (myapp.wsgi_app)
18:26:03.145   myapp.index()
               myapp.index -> <Response 20 bytes [200 OK]> [77us]
             myapp.wsgi_app -> '200 OK' [553us, body 5us over 1 chunk]
18:26:03.145 GET /quote/widget (myapp.wsgi_app)
18:26:03.145   myapp.quoted(item='widget')
18:26:03.146     myapp.quote(item='widget')
                 myapp.quote -> {'item': 'widget', 'price': 25} [13us]
               myapp.quoted -> <Response 29 bytes [200 OK]> [154us]
             myapp.wsgi_app -> '200 OK' [525us, body 3us over 1 chunk]
...
```

The same file traces the development server, with the trees arriving
in its log as requests come in
(`uv run --with flask python -m wrapture --config timing.toml -m flask --app myapp run`),
and adding a second `[[sink]]` of type `jsonlines` streams every event
to a file for step 7's exporters afterwards.

## Where next

- [WSGI request tracing](wsgi-tracing.md) has the full request event,
  the `on_request` namespace and query redaction;
  [ASGI request tracing](asgi-tracing.md) is the async counterpart.
- [Counting without retaining](ad-hoc-tracing.md#counting-without-retaining)
  covers `Aggregate`, which keeps the self-time table for every bound
  path with no events retained, and
  [scheduled tracing](scheduled-tracing.md) turns it into a periodic
  report.
- [Configuring from a file](ad-hoc-tracing.md#configuring-from-a-file)
  has the complete config grammar, including `[[setup]]` hooks like the
  Flask one used above.
