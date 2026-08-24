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
names; `"call"` events become INTERNAL spans named by the binding's
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

`[otel.traces]` takes the span sink's options plus `sample`, a keep
rate applied to the trace export alone, decided once per tree at its
root. Sampling lives here rather than gating the whole registration
deliberately: a gate on everything would starve the metrics
histograms, while sampling inside drops only the span export,
keeping the always-on cheap signal complete beside the sampled
drill-down.

### One trace id everywhere

The sink completes wrapture's trace identity story by claiming the
tree's w3c slot. There are two id generators in the room, wrapture's
minting (which must work with no OTel installed) and the SDK's, and
the sink makes them agree:

- For an identity that arrived in request headers, the root span is
  created with a remote parent built from the slot, so the exported
  tree continues the caller's trace rather than starting a detached
  one, and the arrived sampled flag rides along for the SDK's
  parent-based sampler to honour: an upstream "do not sample"
  exports nothing. The sampler is the SDK default,
  `parentbased_always_on`, overridable through the standard
  `OTEL_TRACES_SAMPLER` variables.
- For an identity wrapture minted locally, the root span is created
  normally, the SDK generating its own trace id, and the slot takes
  the whole identity at that moment, before the operation's body
  runs, so serialised files, outbound headers and exported spans all
  read one id and the backend shows a clean native root.

In both cases the slot's span-id register then tracks the innermost
exported span as spans open and close, so `trace_headers()` carries
a live parent at any moment inside the tree, and downstream services
attach to spans that really got exported. When wrapture's own
`sample =` gate drops a tree, the sink never hears it: the minted id
stands unclaimed and outbound headers carry it, which is what "not
sampled" means.

## The metrics signal

The metrics signal aggregates the same events instead of exporting
them individually: request durations into the semantic-convention
`http.server.request.duration` histogram attributed by method and
status code, call durations into a per-path `wrapture.call.duration`
histogram whose error series split out by exception type (an
escaped exception's, or the first noted with `note_exception()`, so
the error rate counts failures the code handled itself), and a
`wrapture.operations` counter of operations observed beginning. The
bound path is safe as a metric attribute precisely because the
config chose the bindings: the set of values is closed, where the
raw request URL is not, so requests are attributed by method and
status only. The design is the `Aggregate` collector's (bounded
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
deployment overriding what differs. Named keys such as
`export_interval` are passed to constructors explicitly and beat
both spellings.

The SDK posture is wrapture-first: choosing wrapture means taking
all it does, including standing up the tracer, meter and logger
providers, which is what the zero-code story requires, since in the
config-driven case there is no application code to configure the
SDK. An application that already configured its own providers wins
as the failsafe, the telemetry flowing through the exporters the
application chose, and wrapture warns (a `ConfigWarning` per signal)
naming what is lost: the table's provider-level settings no longer
apply, and behaviour wrapture relies on, such as the sampler
honouring an upstream sampling decision, is whatever the application
installed.

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
