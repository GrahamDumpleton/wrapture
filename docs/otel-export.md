# OpenTelemetry export

OpenTelemetry export is a first-class capability of wrapture: the
`wrapture.otel` subpackage turns the recorded event stream into OTel
traces, metrics and logs, sent over OTLP to whatever backend the
standard OTel environment variables name. The pairing with
[trace identity](ad-hoc-tracing.md#trace-identity-and-propagation)
is one deliberate story: the ecosystem converged on one wire format
(W3C trace context) and one export standard (OpenTelemetry), and
wrapture blesses both by name.

The code ships in every wheel; what the `otel` extra adds is the
dependencies:

```console
$ pip install "wrapture[otel]"
```

The extra brings `opentelemetry-sdk` and the http/protobuf OTLP
exporter as the lighter default; the gRPC exporter is used when it
is installed, but is not pulled in. Nothing in base wrapture imports
the subpackage until a config or the application asks for the sink,
so a plain install pays nothing. A package integrating with wrapture
can depend on the string `"wrapture[otel]"` directly, giving its
users a one-string opt-in.

## Enabling from config: the [otel] table

First-class support gets first-class config: the top-level `[otel]`
table, sibling to `[trace]`, is the one registration covering every
signal. Its presence opts in (export needs an endpoint to be useful,
so no table means no export), and `enabled = false` is accepted so a
stanza can be kept in the file but switched off, matching the
`[trace]` style. The `signals` key says which signals are enabled
(all three by default), shared facts sit at the top of the table,
and each signal's own tuning nests beneath it:

```toml
[otel]
service_name = "flask-shop"
signals = ["traces", "metrics", "logs"]

[otel.traces]
sample = 0.1

[otel.metrics]
export_interval = 5

[otel.environment]
exporter_otlp_endpoint = "http://localhost:4318"
exporter_otlp_protocol = "http/protobuf"
```

The sink it builds always registers ahead of whatever the `[[sink]]`
list builds, which then stacks in file order as usual. That ordering
is load-bearing: a tracing sink must hear a root event before any
other sink can observe its trace identity (the claiming story
below), and the table's position makes it true by construction
rather than by convention. Using the table on a plain install fails
the config load with an error naming the fix, installing
`wrapture[otel]`, rather than a bare `ModuleNotFoundError`.

The two neighbouring tables stay crisp: `[trace]` governs
identities, `[otel]` governs export. Any other export destination
remains a `[[sink]]` entry with a `module:attr` factory, which is
also the escape hatch spelling (`type = "wrapture.otel:sink"`) for
composing the OTel sink somewhere unusual, such as inside a window.

In code, the same registration is the `wrapture.otel.sink()`
factory, whose keyword arguments are the table's keys, and the
ordering rule is the caller's, simply stated: add the OTel sink
before other sinks.

```python
import wrapture
import wrapture.otel

wrapture.add_sink(wrapture.otel.sink(service_name="flask-shop"))
```

The factory composes what it is asked for from wrapture's standard
pieces: the span sink wrapped in `Sample` when `traces.sample` is
given, so the trace export is sampled per tree while the metrics
beside it still hear every event, the enabled signals delivered
through a `Fanout`, whose capture negotiation means a metrics-only
registration stays at `"none"` capture while traces raise it to
`"summary"`.

## The traces signal

One event becomes one span. `"request"` events become SERVER spans
named access-log style (`GET /quote/widget`), carrying the method,
path and status code under their semantic-convention attribute
names; a request annotated with its matched `route` (the pattern,
`/quote/<item>`, which framework instrumentation adds once routing
has run) is instead named by that pattern, `GET /quote/<item>`, and
carries it as `http.route`, so a backend groups by endpoint rather
than seeing every URL as a distinct operation (the request data-key
contract below says which keys get this treatment); `"call"` events become INTERNAL spans named by the binding's
assigned label, or by its `module:qualname` path when unnamed, so a
span name with a colon in it always pins the exact function; `"block"`
events become INTERNAL spans named by the block, which
is how an embedded `with wrapture.block("render-invoice"):` shows up
as a span with no OTel API in the code. Attribute events are skipped
as too fine-grained for a trace. Captured arguments, results and
`annotate()` data ride along as span attributes, flattened onto
dotted names (`wrapture.arg.item`, `wrapture.data.rows`), and an
event that closes with an exception ends its span with error status
and the exception recorded. An exception the code caught and noted
with `note_exception()` is recorded the same way, as an exception
event on the span placed at the moment of the note, and sets the
error status unless the span is already in error, so a request's
5xx status and a noted exception agree rather than fight: the
request span shows the response status, the error status, and the
`KeyError` the framework swallowed. Spans are parented through the event
tree's own links, so trees that cross threads export correctly, and
the flush wrapture gives process sinks at interpreter exit drains
the OTel batch processor too, so the tail of a trace is not lost in
its buffer.

Work handed off rather than nested, a thread started with
`wrapture.detach()` or a consumer block naming a message's headers
with `block(links=...)`, exports as a root span of its own carrying
an OTel span link to the origin: the causal-but-not-parent
relationship OTel draws the same way, so a backend shows the
background job as its own trace with a pointer back to the request
that started it, and the request span's duration is the request's
own. The ids on the link are the ones the register held at the
hand-off, the origin's exported span, so the link lands on a span
that exists; a link whose origin ran in another process is marked
remote. Because a detached root starts a fresh trace, the SDK's
parent-based sampler sees no parent and falls back to its root
sampler; a deployment sampling by ratio sets that through the
standard `OTEL_TRACES_SAMPLER` variables, and links themselves
never influence the decision. See
[work the caller does not wait for](ad-hoc-tracing.md#work-the-caller-does-not-wait-for)
for the wrapture side.

An exception reaches the backend as the semantic-convention
exception event: type, message and stacktrace. Messages routinely
embed values (the key a `KeyError` names, the statement a database
driver echoes), so `exceptions` at the top of the `[otel]` table says
how much leaves the process: `"full"` (the default), `"message"`
(type and message, no stacktrace) or `"type"` (the type name alone,
which still sets the error status and the `error.type` metric
dimension). One key governs the traces and logs signals together, so
it cannot be set on one and forgotten on the other; in code it is
`wrapture.otel.sink(exceptions="type")`.

`[otel.traces]` takes the span sink's options plus `sample`, a keep
rate applied to the trace export alone, decided once per tree at its
root. Sampling lives here rather than gating the whole registration
deliberately: a gate on everything would starve the metrics
histograms, while sampling inside drops only the span export,
keeping the always-on cheap signal complete beside the sampled
drill-down. The same table takes `sampler` and `sampler_arg`, the
OTel sampler by its documented name (`"parentbased_traceidratio"`
with a keep rate, say): the config spelling of
`OTEL_TRACES_SAMPLER`, with the difference that it reaches
wrapture's own pipeline alone rather than the process environment.
Where `sample` is wrapture's gate, dropping whole trees before the
sink hears them, `sampler` is OTel's, honouring an upstream decision
carried in inbound headers and leaving the metrics signal complete.

### The request data-key contract

A `"request"` event's descriptive fields live in `event.data`, and
the sinks read a fixed set of keys there specially, mapping each
onto its semantic-convention attribute instead of flattening it
onto a `wrapture.data.*` name. Those keys are reserved on request
events: annotating one for an unrelated purpose produces misleading
telemetry, and annotating one deliberately is the supported way for
request-recording instrumentation of your own (a framework the
pre-built packages do not cover, an in-house one) to opt into the
same treatment without touching any OTel API. The middleware
supplies the first three; the rest are added by instrumentation
inside the request with `annotate()`.

| key | set by | exported as |
|-----|--------|-------------|
| `method` | middleware | `http.request.method`; leads the span name; a metrics dimension |
| `path` | middleware | `url.path`; names the span unless a route is annotated |
| `query` | middleware | `url.query`, after the capture policy's redaction |
| `route` | instrumentation, once routing matches | `http.route`; renames the span to `METHOD route` at close; a metrics dimension |
| `bytes`, `app_duration` | middleware, as the body streams | `wrapture.data.bytes`, `wrapture.data.app_duration`, swept at close |

The result of a request event is its status line, exported as
`http.response.status_code`, with a 5xx setting the span's error
status. Because routing happens after the request event opens, the
route-based name is only known when the span closes; the sampler,
which decides at the root's open, sees the path-based name, the
same as with the SDK's own HTTP instrumentations. A request that
matches no route keeps its path-based name and has no `http.route`.
Every other key on a request event, `endpoint` or anything else an
instrumentation annotates, is exported as ordinary
`wrapture.data.<key>`. Static tags declared up front, `data=` on a
binding or the middleware, or an observe entry's `data` table, are
merged in before the middleware writes the request's own fields, so
a declaration can add ordinary keys but never override a reserved
one. The mapping is scoped to `"request"` events; data on an
uncategorised `"call"` or `"block"` event is never interpreted.

### The category data-key contracts

A binding declared with `category=` (see the [ad-hoc tracing
guide](ad-hoc-tracing.md#terminal-nodes-leaf-and-category)) records
events that carry the category as a field, and the export treats
them as the client side of an exchange. OTel has no category
attribute of its own: a span's kind is structural and its category
is implied by which semantic-convention attributes it carries, so
the export translates rather than passes through. Every categorised
span carries `wrapture.category` verbatim, so a backend that does
not read the conventions can still select on it, and its kind and
attributes follow the category:

| category | span kind | data key | exported as |
|----------|-----------|----------|-------------|
| `external` | CLIENT | `method` | `http.request.method` |
| | | `url` | `url.full` |
| | | `host`, `port` | `server.address`, `server.port` |
| | | `path`, `query` | `url.path`, `url.query` |
| | | `status` | `http.response.status_code`; 400 and above sets the error status, as the HTTP client conventions say |
| | | `system`, `service`, `operation` | `rpc.system`, `rpc.service`, `rpc.method`, for an RPC-shaped external call (an XML-RPC or gRPC client); when `system` and `operation` are both present the span is also named `service/operation` (or `operation` alone), the RPC conventions' low-cardinality naming, with the patched location still on `wrapture.path` |
| `database` | CLIENT | `system` | `db.system.name` |
| | | `operation` | `db.operation.name` |
| | | `collection` | `db.collection.name` |
| | | `statement` | `db.query.text`; only ever present when the instrumentation chose to capture it |
| `datastore` | CLIENT | `system`, `operation`, `collection` | as for `database` |
| `messaging`, `task` | PRODUCER | `system` | `messaging.system` |
| | | `destination` | `messaging.destination.name` |
| | | `operation` | `messaging.operation.type` |
| `template` | INTERNAL | | `wrapture.category` only |

The RPC trio follows OTel's RPC conventions, younger than the HTTP
ones but with these three names stable across their drafts; an
instrumentation that sets `operation` should set `system` beside it,
the RPC span's marker attribute.

Instrumentation fills the contract keys with `annotate()` from
inside the operation (a leaf's body annotates the leaf, since
nothing beneath it is in flight) or with `data=` where a value is
fixed for the target, the driver's `system` say. A `status` may be
an integer or a status line; a `url` should carry no query string,
which goes under `query` as `wrapture.capture_query()` records it,
the same by-name redaction the request middlewares apply. Keys
outside the category's contract,
and contract keys of some other category, export as ordinary
`wrapture.data.<key>`. The leaf categories describe the client or
producing side only: consuming a message or running a queued task
roots a tree of its own, structurally a request, and is a root event
kind question for when such a target is instrumented.

### One trace id everywhere

The sink completes wrapture's trace identity story by claiming the
tree's w3c slot. The identity exported is the slot's own; the sink
mints no trace id of its own, so there is nothing to reconcile:

- For an identity wrapture minted locally, the root span carries the
  minted trace id and takes the minted span id as its own, and the
  slot is marked claimed, so serialised files, outbound headers and
  exported spans all read one id and the backend shows a clean
  native root.
- For an identity that arrived in request headers, the root span
  continues the caller's trace with a remote parent built from the
  slot, so the exported tree joins the caller's rather than starting
  a detached one. The arrived sampled flag rides on that parent for
  the parent-based sampler to honour: an upstream "do not sample"
  exports nothing.

The sampler decides once per tree, at the root, and the spans
beneath inherit the decision. It is `parentbased_always_on` by
default, overridable by the `sampler` and `sampler_arg` keys of
`[otel.traces]` or the standard `OTEL_TRACES_SAMPLER` and
`OTEL_TRACES_SAMPLER_ARG` variables, either naming the SDK's
documented samplers (`always_on`, `always_off`, `traceidratio`, and
their `parentbased_` forms). A tree the sampler drops exports no spans but
is still claimed, its slot recording the decision, so outbound
headers tell downstream services "not sampled" and the trace stays
consistently absent end to end.

In both cases the slot's span-id register then tracks the innermost
exported span as spans open and close, so `trace_headers()` carries
a live parent at any moment inside the tree, and downstream services
attach to spans that really got exported. When wrapture's own
`sample =` gate drops a tree, the sink never hears it: the minted id
stands unclaimed and outbound headers carry it, which is what "not
sampled" means.

### How a span is built

The sink does not use the SDK's tracer. The SDK's own pipeline is
layered: a tracer builds a mutable span, validating each attribute
as it is set; ending the span snapshots it as a `ReadableSpan` and
hands it to the span processor; the processor batches and the
exporter encodes and sends. Everything a wrapture span needs is
known when its event closes, so the sink builds the `ReadableSpan`
itself at that moment, in one step from the event, and hands it to
the same processor. What the sink keeps between an event's enter
and its close is a small entry holding the ids, the parent and the
start time. The processor, exporter and wire format are the SDK's,
unchanged; only the tracer's intermediate span object, with its
validated attribute store and its locks, is skipped, which is where
most of the per-span cost of exporting used to go. The exception
stacktrace attribute is formatted from a plain walk of the
traceback's frames rather than `traceback.format_exception`, which
on current Pythons also parses each frame's source to draw caret
underlines no backend renders.

### What it costs

The point of building spans this way is that exporting through
wrapture should cost no more than instrumenting with OpenTelemetry
directly, and the comparison is worth stating in numbers, since the
overhead of Python instrumentation is the usual objection to it.
Measured in August 2026 against wrapture 1.0.0a10 on Python 3.14 on a
MacBook Air M4, best of five timeit runs of 100,000 calls, with every
column exporting through the same `BatchSpanProcessor` to an exporter
that discards what it receives, microseconds per call:

| | root call | nested, 3 spans | call that raises |
|---|---:|---:|---:|
| wrapture: binding recorded and exported by the span sink | 7.2 | 19.9 | 28.7 |
| OTel SDK: `start_as_current_span()` and nothing else | 5.8 | 18.6 | 99.9 |
| OTel instrumentation package style: wrapt wrapper, `code.function`, `code.namespace` and `code.lineno` set at start, exception recorded by the context manager | 7.0 | 22.5 | 105.4 |
| OTel wrapper collecting what wrapture collects: path, kind, sequence number, thread name, arguments bound to their names with a bounded repr, result, exception | 9.0 | 28.7 | 84.1 |

Of wrapture's 7.2 us, about 3.2 us is recording (the event, its
normalized arguments, delivery to the sink) and about 4 us is the span
sink, against about 6 us for the SDK's tracer producing the same span
and about 1 us for the bare hand-off of a finished span to the
processor. The raising call is where the difference is largest: the
SDK's `record_exception` formats the stacktrace with
`traceback.format_exception`, which on Python 3.11 and later parses
each frame's source to draw caret underlines, and the sink formats the
same frames without them. Outside the OTel path, a binding costs about
70 ns per call while suspended and about 0.5 us while active with
nothing listening. The figures show relative cost on one machine at
one point in time and are not a guarantee; a later measurement may be
taken on different hardware, so compare the columns with each other
rather than any one of them with a number from elsewhere.

## The metrics signal

The metrics signal aggregates the same events instead of exporting
them individually: request durations into the semantic-convention
`http.server.request.duration` histogram attributed by method,
status code and, for a request annotated with its matched `route`,
`http.route`, so per-endpoint latency reads straight off the
histogram; call durations into a per-path `wrapture.call.duration`
histogram whose error series split out by exception type (an
escaped exception's, or the first noted with `note_exception()`, so
the error rate counts failures the code handled itself), and a
`wrapture.operations` counter of operations observed beginning. The
bound path is safe as a metric attribute precisely because the
config chose the bindings: the set of values is closed, where the
raw request URL is not, so requests are attributed by method and
status, and by the route pattern, closed the same way, never the
URL. The design is the `Aggregate` collector's (bounded
memory, `"none"` capture on both axes, so nothing is ever retained
or even captured) with the aggregation handed to the OTel SDK.
`[otel.metrics]` takes `export_interval`, seconds between metric
exports, the config file's spelling of
`OTEL_METRIC_EXPORT_INTERVAL`.

## The logs signal

The logs signal exports the log events the
[`[[log]]` captures](ad-hoc-tracing.md#configuring-from-a-file)
select, through the OTel logs bridge. Selection stays where it is:
the captures decide which messages become events at all, and the
sink only exports events that exist. The mapping is direct, severity
from the record's level, body from the formatted message, the logger
name and source location as attributes, the logged exception as the
standard exception attributes. The payoff of log events inheriting
their tree's trace is correlation: each record lands carrying the
tree's trace id and the id of the exported span it happened inside,
so opening a request's span in the backend shows its log lines, not
just its timings.

## Providers, the environment, and who wins

`[otel.environment]` holds defaults for OTel's own environment
variables: each key is uppercased, prefixed with `OTEL_` when not
already, and applied with setdefault before the providers are built,
so the file can name any of the SDK's documented variables without
wrapture knowing them individually, and a variable set in the real
environment always wins. That gives the file three postures:
self-contained (endpoint in the file, runs with no environment
setup), deployment-owned (no environment table, the real environment
decides everything), or mixed, defaults in the file with the
deployment overriding what differs. The variables are process-wide:
anything else in the application that builds OTel providers or
exporters from the environment sees the file's defaults too, which
is what makes the table convenient and is worth knowing about when
an application has OTel setup of its own. Named keys such as
`export_interval` and `sampler` are passed to constructors
explicitly, beat both spellings, and touch nothing shared.

The SDK posture is wrapture-first: choosing wrapture means taking
all it does, including standing up the export pipelines, which is
what the zero-code story requires, since in the config-driven case
there is no application code to configure the SDK. The pipelines are
wrapture's own: the span processor and sampler for traces, and the
meter and logger providers for the other signals, are built from the
table and the environment and handed to wrapture's sinks directly.
The SDK's global providers are neither consulted nor installed, so
an application that uses the OpenTelemetry API on its own account
keeps whatever it set up for itself, its spans going wherever it
sends them, and wrapture's telemetry is unaffected by it. The two do
not interleave in one pipeline; they were never parented together in
any case, since wrapture parents through its own event tree rather
than OTel's ambient context.

For a look without a collector, `OTEL_TRACES_EXPORTER=console`,
`OTEL_METRICS_EXPORTER=console` and `OTEL_LOGS_EXPORTER=console`
swap the OTLP exporter for standard output, per signal. The
flask-app example in the source checkout runs this way with no
environment setup at all; its README carries the run commands:

```console
$ cd examples/flask-app
$ uv run --with flask --extra otel \
    python -m wrapture --config wrapture-otel.toml main.py
```

Each request then arrives in the backend as one trace: a SERVER
root span named for the request line, the view handler and its
helpers nested beneath it with their captured arguments and
results, and a failing request's tree marked as an error with the
exception recorded on the span that raised it.

## Workers that fork

The sink participates in wrapture's
[fork story](ad-hoc-tracing.md#forked-worker-processes): in a forked
child its `on_fork()` drops the open-span table, since the in-flight
spans belong to the parent, which will close them, while the OTel
SDK's own at-fork handling restarts the exporter worker threads. The
child's first operation is a genuine root, a daemon worker's next
request mints or joins a trace as any root request does, and a child
wanting an immediate identity opens a block.
