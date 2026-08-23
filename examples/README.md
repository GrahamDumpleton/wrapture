# wrapture examples

Small, runnable demonstrations of zero-code tracing with the
`python -m wrapture` runner and the `python -m wrapture.tools`
commands. Each subdirectory is self-contained: the code being
observed, an entry script, and the `wrapture.toml` the runner picks
up from the working directory.

In every example the observed code never imports wrapture. The entry
script imports it from a sibling module, and the runner applies the
config before the script runs, so the patches are in place before
that import happens. That split matters when writing your own: the
runner executes the entry script under the name `__main__`, so
members to observe must live in an importable module, not in the
entry script itself.

## Running

From a checkout of this repository, uv runs everything against the
project environment. Change into an example directory first, since
the runner finds `wrapture.toml` in the current directory:

```console
$ cd examples/live-printer
$ uv run python -m wrapture main.py
```

With wrapture installed into an environment of your own, drop the
`uv run` prefix and use plain `python -m wrapture main.py` anywhere
below.

## Running through autowrapt instead

Every example also runs through the injection path, where no
launcher is involved: autowrapt fires wrapture's bootstrap at
interpreter startup, the same config is discovered from the working
directory, and the program is started with plain `python`. From the
checkout, uv supplies autowrapt for just that run:

```console
$ cd examples/live-printer
$ AUTOWRAPT_BOOTSTRAP=wrapture uv run --with autowrapt python main.py
```

With wrapture and autowrapt both installed in an environment of your
own, the same is:

```console
$ AUTOWRAPT_BOOTSTRAP=wrapture python main.py
```

The output is identical to the runner's, because the runner and
autowrapt are two triggers for the same config machinery. The config
applies during interpreter startup, but observe entries defer:
bindings land when the application itself imports the observed
module, so nothing has to be importable that early. Only
operator-code carries a `pythonpath` entry, because its sink factory
resolves when the config loads and lives in an uninstalled package
next to it.

## live-printer

The quickest look at what observing feels like. A shop places three
orders, one of which the payment gateway declines. The config binds
the order flow (`name` for exact members, `match` with an exclude for
the gateway) to a `printer` sink, so the call tree prints live to
stderr while the orders run: one line as each call begins, indented
by nesting, `->` lines for results and a `!!` line where the
declined card raises. A `[[log]]` entry captures the shop's own log
messages as events too, so the warning the service logs for the
declined order prints in place, nested inside the call that logged
it. (The same message also appears unindented, once: that is the
logging module's own last-resort handler doing its normal job, since
the demo configures no handlers; capture never disturbs delivery.)

```console
$ cd examples/live-printer
$ uv run python -m wrapture main.py
```

## stream-to-disk

A pipeline processes four sources on two worker threads while a
`jsonlines` sink streams every completed event to `trace.jsonl`. A
config sink is a process sink, so it hears all the threads with no
timeline anywhere. The sink appends across runs, so delete the file
first for a clean trace:

```console
$ cd examples/stream-to-disk
$ rm -f trace.jsonl
$ uv run python -m wrapture main.py
```

Then render the trace. For a timeline, convert to Chrome trace JSON
and drop the result onto <https://ui.perfetto.dev>: one lane per
worker thread, nested slices per call, and clicking a slice shows
the captured arguments and result:

```console
$ uv run python -m wrapture.tools convert --format chrome -o trace.json trace.jsonl
```

The other formats write to standard output, for pasting into a
GitHub comment or comparing as a snapshot:

```console
$ uv run python -m wrapture.tools convert --format mermaid trace.jsonl
$ uv run python -m wrapture.tools convert --format canonical trace.jsonl
```

## operator-code

The config extensibility story in one directory: everything beyond
plain observation lives in an ordinary Python package next to the
config file, reached only by the references in `wrapture.toml`.

- `pythonpath = "."` is anchored to the config file's directory, so
  the `wrapture_local` package beside it is importable however the
  process was launched.
- The `[[setup]]` entry names a callback that runs when the `shop`
  module is imported; it binds the gateway with a `when=` predicate,
  and the entry's extra `threshold` key reaches the callback as a
  keyword argument, so of the six charges made only the two over 400
  are recorded, with the cutoff adjustable in the config file alone.
- Two `[[sink]]` entries fan out to a live printer and a JSONLines
  file; a relative sink path is anchored to the config file's
  directory just as `pythonpath` is.

```console
$ cd examples/operator-code
$ rm -f trace.jsonl
$ uv run python -m wrapture main.py
```

Two charges print live and land in `trace.jsonl`; convert the file
as in the previous example to see the same two from the other
renderings. Edit `threshold` in `wrapture.toml` and run again to
watch the selection change without touching any Python.

## flask-app

The out-of-box APM experience: a small Flask shop whose code never
mentions wrapture, and a config that never names the app, where
every HTTP request prints as one tree, the request line, the view
handler and its helpers nested beneath it, and the status when the
body closes.

All the Flask knowledge lives in one setup hook
(`wrapture_local/flask_support.py`), triggered by the import of
`flask` itself, a stand-in for what a wrapture-flask package would
ship; packaged with an entry point, the config would say `[[setup]]`
with `group = "wrapture_flask"` and nothing else. The hook patches
Flask's two choke points:

- `Flask.__init__` installs the recording WSGI middleware on each
  new instance's `wsgi_app` attribute, so every application the
  process creates is covered however it was made, application
  factories included, labelled with the app's own name.
- `Flask.add_url_rule` substitutes `wrapture.observed(view_func)` as
  routes register. Flask captures view functions into its dispatch
  table the moment `@app.route` runs, before any observation could
  exist, so the hook intercepts registration itself; every route
  registered afterwards is captured, wherever the view came from:
  module functions, closures, blueprints from other modules.

The one observe entry in `wrapture.toml` covers this application's
own `quote` helper, the kind of addition an operator layers on top
of the out-of-box instrumentation.

Flask is not a dependency of this repository, so uv supplies it for
the run:

```console
$ cd examples/flask-app
$ uv run --with flask python -m wrapture main.py
```

The script drives four requests through Flask's test client,
including one that fails, so the last tree shows the `KeyError`
inside the handler and the `500` the request becomes. The same
config also traces the real development server, with the trees
appearing in the server log as requests arrive:

```console
$ uv run --with flask python -m wrapture -m flask --app myapp run
$ curl http://127.0.0.1:5000/quote/widget
```

### Exporting to OpenTelemetry

The directory carries a second config, `wrapture-otel.toml`, that
adds two OpenTelemetry sinks alongside the printer: the same request
trees, exported as OTel traces and aggregated into OTel metrics.
Each request becomes a SERVER span named access-log style with the
usual HTTP attributes, the view handler and its helpers become
INTERNAL spans beneath it, and the failing request's tree arrives
with error status and the recorded `KeyError`. The metrics sink
feeds the same events into the semantic-convention
`http.server.request.duration` histogram by method and status, a
per-path `wrapture.call.duration` histogram whose error series split
out by exception type, and a counter of operations begun.

Both sinks ship in wrapture itself, as the `wrapture.otel`
subpackage, with the OpenTelemetry dependencies behind the
`wrapture[otel]` extra; this example is a plain consumer of the
shipped code, and the OpenTelemetry export page of the
documentation is the full guide. The top-level `[otel]` table
registers the sinks: its `signals` key says which signals are on,
shared facts like `service_name` sit at the top of the table, and
per-signal tuning nests beneath it (`[otel.metrics]` sets
`export_interval = 5`, seconds between metric exports; an
`[otel.traces]` table would take `sample = 0.1` to sample the trace
export alone while the metrics still hear every event). The
`[otel.environment]` table supplies defaults for OTel's own
environment variables, each key uppercasing to its `OTEL_*` name and
applied with setdefault. The shipped file defaults to a local
collector on OTLP over http/protobuf, so the demo needs no
environment setup at all:

```console
$ uv run --with flask --extra otel \
    python -m wrapture --config wrapture-otel.toml main.py
```

Because the file's entries are only defaults, a variable set in the
real environment always wins; pointing the same unchanged config at
a gRPC collector is the demonstration:

```console
$ export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317"
$ export OTEL_EXPORTER_OTLP_PROTOCOL="grpc"
```

The four scripted requests make a short burst. For a sustained
stream of metrics, run the development server under the same config
and drive traffic at it from a second shell; the config's
five-second `export_interval` means points arrive while the traffic
runs. The explicit port stays clear of macOS AirPlay, which listens
on 5000:

```console
$ uv run --with flask --extra otel \
    python -m wrapture --config wrapture-otel.toml -m flask --app myapp run --port 5001
```

Then, from the second shell, thirty seconds of mixed traffic,
successes and the failing item alike, a few dozen requests a second:

```console
$ end=$((SECONDS + 30)); while [ "$SECONDS" -lt "$end" ]; do
    for path in / /quote/widget /quote/gadget /export /quote/missing; do
      curl -s -o /dev/null "http://127.0.0.1:5001$path"
    done
    sleep 0.2
  done
```

Each interval then lands another point on the histograms: the
request duration series by status code climbing in step, the
per-path call durations beneath them, and the `KeyError` series
growing at exactly the rate of the `/quote/missing` hits.

With no collector at hand, `OTEL_TRACES_EXPORTER=console` and
`OTEL_METRICS_EXPORTER=console` dump the spans and metrics to
standard output instead:

```console
$ OTEL_TRACES_EXPORTER=console OTEL_METRICS_EXPORTER=console \
    uv run --with flask --extra otel \
    python -m wrapture --config wrapture-otel.toml main.py
```

If the application had already configured its own provider, it
would win as the failsafe, the spans flowing through the
application's exporter with a warning naming what of the `[otel]`
table no longer applies; here the app knows nothing of OTel, so the
zero-code path stands the providers up itself.

## fastapi-app

The same out-of-box APM experience for the async world: the same
small shop as flask-app, written with FastAPI, whose code never
mentions wrapture and whose config never names the app.

All the FastAPI knowledge lives in one setup hook
(`wrapture_local/fastapi_support.py`), triggered by the import of
`fastapi` itself, the stand-in for a wrapture-fastapi package. The
choke points differ from Flask's because a FastAPI instance is
itself the ASGI application, with no swappable attribute like
`wsgi_app`:

- `FastAPI.build_middleware_stack` wraps the middleware pipeline the
  instance builds once, lazily, at its first request, in the
  recording ASGI middleware, so every request records as one tree
  whatever middleware the app added, labelled with the app's own
  title.
- `APIRouter.add_api_route` substitutes
  `wrapture.observed(endpoint)` as routes register, which every
  registration form funnels through: decorators on the app,
  `APIRouter` instances, `include_router` from other modules.
  FastAPI's dependency-injection introspection of the endpoint
  signature sees straight through the observer proxy, and sync
  endpoints record correctly from the worker threads FastAPI runs
  them in.

FastAPI is not a dependency of this repository, so uv supplies it
(and httpx, for the test client) for the run:

```console
$ cd examples/fastapi-app
$ uv run --with fastapi --with httpx python -m wrapture main.py
```

The script drives four requests through FastAPI's test client,
including one that fails, so the last tree shows the `KeyError`
escaping the handler while the client still receives the `500` the
framework makes of it. The same config also traces a real server,
with the trees appearing in the server log as requests arrive:

```console
$ uv run --with fastapi --with uvicorn python -m wrapture -m uvicorn myapp:app
$ curl http://127.0.0.1:8000/quote/widget
```

## trace-propagation

Two processes, one trace. A client places orders against a quote
service over HTTP, both sides observed by wrapture, and the trace
identity minted at each client tree's
root travels in the `traceparent` header and reappears in the
server's records: distributed tracing with no tracing backend
anywhere, just wrapture on both ends. The server code never
mentions wrapture; the client's one embedded touch is a pair of
`wrapture.block()` markers in `fetch_quote`, splitting the exchange
into making the request and consuming the reply body, the two
phases urllib's `open()` cannot separate by itself since the
response object escapes the call with its body unread.

The moving parts are the mechanism's defaults plus one probe. Trace
identity is on by default, so every client tree mints an id at its
root. The setup hook in `wrapture_local/urllib_support.py`, the
stand-in for what a wrapture-probe-urllib package would ship, binds
urllib's opener so each outbound request records as a client-side
call and carries `wrapture.trace_headers()` in its headers, the
whole public surface a probe needs. On the server, the WSGI
middleware parses the incoming header at the boundary, so that
process's trees join the client's trace instead of minting their
own.

Everything is standard library, so no extra packages are involved.
First terminal, the server:

```console
$ cd examples/trace-propagation
$ rm -f client.jsonl server.jsonl
$ uv run python -m wrapture --config server.toml server.py
```

Second terminal, the client:

```console
$ cd examples/trace-propagation
$ uv run python -m wrapture --config client.toml client.py
```

Each order prints as one tree in each terminal. The join is in the
files both configs also stream: every line carries its tree's trace
id, and the same ids appear in both, client and server halves of one
distributed trace:

```console
$ uv run python -c "
import json
for name in ('client.jsonl', 'server.jsonl'):
    for line in open(name):
        record = json.loads(line)
        print(record['trace']['w3c']['trace_id'][:8], name, record['path'])
" | sort
```

A request arriving with no `traceparent` (a plain `curl` at the
server) mints a fresh id at the boundary instead, so the server's
records are joinable per request either way.

The directory carries a second pair of configs, `client-otel.toml`
and `server-otel.toml`, that add OpenTelemetry export on both sides
(the `wrapture[otel]` extra supplies the dependencies), which turns
the same run into the cross-service correlation demo. The client
side's sink claims each tree's identity, so the id the probe
injects is the id of a span the backend really has; the server
side's sink creates each request span with the arrived identity as
a remote parent. In an OTel viewer each order is then one
distributed trace: the `order-frontend` spans with the
`quote-service` request span attached beneath the outbound urllib
span that made the call. Same two terminals, with the extra:

```console
$ uv run --extra otel python -m wrapture --config server-otel.toml server.py
$ uv run --extra otel python -m wrapture --config client-otel.toml client.py
```

The configs also stream `client-otel.jsonl` and `server-otel.jsonl`,
and the join query above works on them unchanged; the ids it prints
are now the claimed ones, the same ids the viewer shows, files,
headers and spans agreeing throughout.
