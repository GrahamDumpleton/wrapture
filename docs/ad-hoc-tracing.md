# Ad-hoc tracing

The same bindings that drive tests can stream a live, structured trace
out of a running application. Nothing about a binding is test-specific:
it observes a call site and emits events, and what happens to those
events is decided by whoever is listening. This page covers that
listening side.

The running example throughout is the inherited dev server: an
application you did not write and would rather not modify, whose
behaviour you need to see. The page builds the answer up in layers:
the sink protocol and its two registration tiers; a small library of
sinks that print, count and stream; narrowing at the binding with
`when=`; streaming to disk as JSON Lines; the TOML config file and
the `python -m wrapture` runner that make the whole intervention
zero-code; and the exporters that render a recorded trace for
Perfetto, snapshot comparisons and Mermaid diagrams.

## Sinks: where events go

Binding points emit events; sinks consume them. The recording gate is
"is anything listening", not "is there a timeline": a `Tape` scoped to
a test is one kind of listener, and a sink registered for the life of
the process is another. When nothing is listening, an applied binding
constructs no event at all, so a bound but unmonitored call site costs
almost nothing beyond wrapt's own dispatch.

A sink subclasses `wrapture.Sink` and overrides the notifications it
cares about:

```python
class Sink:
    def on_enter(self, event): ...   # recorded, operation not yet run
    def on_exit(self, event): ...    # operation completed
    def on_error(self, event): ...   # operation raised
    def flush(self): ...             # push buffered output out
```

Each event is heard at most twice: `on_enter` when it is recorded, with
its position (sequence number, depth, parent link) already assigned and
its outcome fields still unset, then exactly one of `on_exit` or
`on_error` when the operation completes or raises. The event's `kind`
field says what was observed (a call, or an attribute get, set or
delete), so the protocol stays three notifications however many kinds
exist. An event that is never closed, such as a generator abandoned
mid-iteration, gets an enter and no exit, which is itself information.

Two properties of delivery matter to sink authors:

- **Notifications run inline**, on the thread that executed the
  observed operation, so they should be quick. Code a sink calls is
  never itself recorded, so a sink can safely touch observed objects
  without recording recursively.
- **A sink can never take the application down.** An exception raised
  from a notification is suppressed, counted on the sink's `errors`
  attribute, and reported with a `SinkErrorWarning` the first time
  only, so a sink broken in a hot loop cannot flood the warnings.

A sink also declares, through `capture_args` and `capture_result`, how
much of each event's values it needs, using the same levels the
[capture policies](unit-testing.md) define. The
effective level for a recorded event is the highest any active sink
declares, so a test's tape (which declares `"reference"`) never
downgrades what a streaming sink sharing the process needs, and a
binding's own `capture=` override still beats them all.

## Process and scoped listening

Sinks are registered on one of two tiers, and the difference is who can
hear:

- **Scoped sinks** live in a context variable. A timeline's tape is
  one: entering `with timeline(...)` pushes the tape, exiting removes
  it, and the tape is visible only to the context that opened it. That
  is what keeps one test's recording out of another's, and it
  propagates the way context does: into asyncio tasks, but not into
  threads.
- **Process sinks** are registered with `add_sink()` and hear every
  recorded event, from every thread and task, until `remove_sink()`.
  This is the tier for observing a whole running application, where
  there is no enclosing scope.

```python
printer = wrapture.add_sink(wrapture.Printer())
...
wrapture.remove_sink(printer)
```

Because the process tier is global rather than context-bound, a worker
thread's calls reach it with nesting intact, even though the same calls
are invisible to a timeline; the
[thread limitation](known-limitations.md#calls-on-other-threads-may-not-be-recorded)
is a property of scoped recording, not of recording itself.

At interpreter exit wrapture delivers everything it still owes:
`flush()` is called on every process sink still registered, so the
tail of a trace, usually the interesting part, is not lost in a
sink's buffers, and any [window](scheduled-tracing.md) with a run open closes
it and writes its reports. `wrapture.shutdown()` performs exactly
that operation on demand, and it is the one call to know for
environments that tear the interpreter down without ever running
atexit callbacks: embedded interpreters and subinterpreters are
destroyed by their host, and hosting platforms typically offer their
own shutdown notification instead. Under mod_wsgi, for example,
subscribe `shutdown()` to the process shutdown event, which fires
while the interpreter and its threads are still fully alive. Calling
it more than once is safe, nothing is uninstalled (bindings stay
applied, so it quiesces rather than tears out the tracing), and a
step that fails is warned about while the rest still run.
`flush_sinks()` remains as the sink half alone, for putting a trace
on disk mid-run.

## Watching calls live: Printer

`Printer` is the simplest real sink: it prints each event as it
happens, one line when an operation begins, indented by nesting depth,
and a closing line with the outcome and how long it took, using the
same `->` and `!!` markers as `tape.tree()`. It writes to the stream
you give it, or to `sys.stderr` by default, flushing every line so the
trace is intact up to the moment of a crash or hang.

```pycon
>>> import sys
>>> import wrapture

>>> class Gateway:
...     def charge(self, amount, currency="USD"):
...         return f"ch_{amount}"

>>> charge = wrapture.binding(Gateway, "charge").apply()
>>> printer = wrapture.add_sink(wrapture.Printer(sys.stdout))

>>> Gateway().charge(500)
Gateway.charge(amount=500, currency='USD')
Gateway.charge -> 'ch_500' [...]
'ch_500'

>>> wrapture.remove_sink(printer)
>>> _ = charge.remove()

```

The bracketed figure on the closing line is the elapsed time, in the
same adaptive units `tape.tree(times=True)` uses (`12us`, `3.2ms`,
`1.40s`), and for a streamed body (a generator, a request) it also
shows the time spent producing the body and how many items or chunks
there were: `[11.8ms, body 4.1ms over 3 chunks]`. An operation whose
result was not captured still gets a closing line with the time. Pass
`timing=False` when stable output matters more than the figure, as in
a doctest or a diff.

Two more options make the printer useful away from a terminal. `path`
appends to a file instead of a stream (opened on the first line
written, and closed by `close()`; the file, being a plain path, is
also what a config file can name). `timestamps=True` prefixes each
opening line with the local wall-clock time to the millisecond, so a
printer file can be lined up with server logs afterwards. Closing
lines are not timestamped; the opening time plus the duration locates
them, and it keeps the tree readable:

```text
14:05:30.117 GET /orders/42?expand=items (myapp.wsgi.application)
14:05:30.118   shop.OrderService.load(order_id=42)
               shop.OrderService.load -> <Order 42> [3.2ms]
             myapp.wsgi.application -> '200 OK' [11.8ms, body 4.1ms over 3 chunks]
```

```python
wrapture.add_sink(wrapture.Printer(path="trace.log", timestamps=True))
```

`stream` and `path` are mutually exclusive; with neither, output goes
to `sys.stderr`. The path is an output path template like the
JSONLines one, so `path="logs/trace-{date}.log"` with `rotate="1d"`
gives one printer file per day; see
[Output paths and rotation](#output-paths-and-rotation) below.

No timeline appears anywhere above: the binding is applied, a sink is
listening, and events flow. This is the minimal form of tracing a
running application: name the methods that matter in the application's
entry point, register a sink, and a nested trace appears as requests
come in, with no timeline and no changes to the code being observed.

`Printer` is the live view; `tape.tree()` is the tidy reconstruction
after the fact. In a test, both can run at once, since sinks compose:
keep the timeline for assertions and add a `Printer` while debugging.

A captured log message (a `"log"` event, from the `[[log]]` config
entry or `capture_logs()`) prints as exactly one line in the tree,
the message repr-escaped so an embedded newline cannot break the
indentation, with the `!!` marker for a logged exception on the same
line: `log myapp.orders ERROR 'charge failed' !! ConnectionError`.
Tracebacks never print, here or anywhere in the printer; the
exception object rides the event for the consumers built to hold it.

## Counting without retaining

Not every question needs the events themselves; often a number is the
answer. Two **collectors** keep numbers and nothing else, so they are
safe to leave running for a whole test suite or a long-lived process:

- `Counter()` counts operations as they begin, failures included. One
  number, no retention.
- `Aggregate()` keeps one row per path: how many operations began,
  how many completed and how many of those raised, and the total,
  self, fastest and slowest execution times of the completed ones.
  Memory is bounded by the number of bound locations, however many
  events flow.

Both are collectors in the sense of the [scheduled tracing page](scheduled-tracing.md):
placed in a window they accumulate while a run is open and hand back
a report when it closes, which is how "a summary every hour" or "one
report for the whole process" is spelled, in code or in a config file.
Both are also sinks, hearing events while registered, so in code they
can equally be registered with `add_sink()` and read directly. Both
declare `"none"` on both capture axes, which matters: when no other
active sink asks for more, recording skips value capture entirely,
including signature binding, the dominant cost of recording a call. A
counter over a hot method costs a fraction of what a recording tape
does.

Counting across a whole suite is how the classic N+1 query regression
gets caught. Bind the database layer's entry point once, register a
`Counter`, and give every test a query budget:

```python
# conftest.py
import pytest
import wrapture

from myapp.database import Database

queries = wrapture.Counter()

def pytest_configure(config):
    wrapture.binding(Database, "execute").apply()
    wrapture.add_sink(queries)

@pytest.fixture(autouse=True)
def query_budget():
    before = queries.count
    yield
    used = queries.count - before
    assert used <= 25, f"test issued {used} queries; N+1 regression?"
```

A test that quietly starts issuing a query per row now fails with a
number attached, and because the counter asks for no values, the whole
suite pays almost nothing for the guarantee.

`Aggregate` answers the follow-up question, "where is the time going",
without retaining a single event. Its `stats` are there to read at
any time, and its report renders them as a table:

```python
with wrapture.window(collect=[wrapture.Aggregate()]) as run:
    drive_traffic()

print(run.reports[0].text)
```

```text
aggregate "aggregate" run 1, 2026-08-18 14:05:30 to 14:06:00 +10:00 (30.0s), pid 4142
3 paths, 153 operations begun, 153 completed, 3 raised

calls    total     self  per-call    min    max  errors  path
  100  114.0ms  114.0ms     1.1ms  1.0ms  1.9ms          shop:Gateway.charge
   50  118.2ms    4.2ms     2.4ms  2.1ms  3.4ms          shop:Processor.process
    3     24us     24us       8us    2us   14us       3  shop:Gateway.refund
```

`self` is the figure profilers rank by, and the table is sorted by
it: the time spent in the operation itself, excluding the time its
observed children account for. A method that is slow because of what
it calls ranks low on self time; a method that is slow in its own
right ranks high, and that distinction is what tells you where to
look. No external profiler can compute it for an arbitrary handful of
bindings, because it only sees whole call stacks; wrapture computes it
from the parent links as events close, retaining nothing.

Every recorded event carries `started` and `duration` (exceptions
included), which is what the rows are built from. For generators the
accumulated body time stands in for the wall duration, since the wall
figure includes the consumer's time between yields, which is not the
generator's to spend. The same numbers are available on a tape:
`tape.self_time(event)` for one event, and `tape.tree(times=True)`
for the whole picture, covered on the
[unit testing page](unit-testing.md).

## Composing sinks: fan-out, sampling and filtering

Combinators make sinks compose like building blocks. Each is itself a
sink wrapping others, so they nest freely:

- `Fanout(*sinks)` delivers every notification to several sinks, with
  the same isolation the registry gives top-level sinks: one broken
  inner sink is counted and skipped, its siblings still hear the event.
- `Filter(predicate, sink)` forwards only events the predicate
  accepts. The predicate is consulted once, when the event enters, and
  the decision sticks, so the inner sink always sees properly paired
  enter and exit notifications even for fields that change as the
  operation completes.
- `Depth(max_depth, sink)` forwards only the top levels of the call
  tree: `Depth(1, ...)` is roots only, `Depth(2, ...)` roots and their
  direct children.
- `Sample(rate, sink)` keeps a random fraction of whole call trees.
  The decision is made once per tree, at its root, and inherited by
  everything beneath, because sampling per event would emit children
  whose parents were never seen.

One registration can therefore serve several needs at different costs:

```python
wrapture.add_sink(wrapture.Fanout(
    wrapture.Aggregate(),                      # always on, numbers only
    wrapture.Depth(2, wrapture.Printer()),     # live view, top of tree
    wrapture.Sample(0.01, detailed_sink),      # 1% of trees, in full
))
```

A combinator declares the capture levels of what it wraps (`Fanout`
takes the highest of its inner sinks'), read at construction, so
capture negotiation sees through the composition and the aggregate
above never forces values to be captured for the printer's sake.

## Deciding at the binding: when=

Sink-side narrowing with `Filter` or `Sample` happens after an event
has been constructed and its values captured; the recording cost is
already paid by the time the sink declines it. When the point is to
keep a hot binding cheap, decide before any of that exists:

```python
charge = wrapture.binding(
    PaymentGateway, "charge",
    when=lambda instance, args, kwargs: kwargs.get("tenant") == "acme",
)
```

The predicate is consulted once per operation, only while something is
listening, and a falsey answer means no event is constructed at all:
no signature binding, no capture, no delivery. The operation itself
still runs and behaviour still applies, and the skip is counted on the
binding's `filtered_calls`, so a shorter trace than expected can be
explained rather than guessed at. On an attribute binding a set passes
the written value as the one positional argument and a get or delete
passes empty args, the same mapping onto call shape the behaviour
pipeline uses. For a generator target the decision is made once, at
the call, and covers the whole iteration.

Because the answer comes from a callable, adjusting the filter at
runtime needs no API: write the predicate to consult state you
control, and change that state while the process runs.

```python
TRACED_TENANTS: set[str] = set()

def traced_tenant(instance, args, kwargs):
    return kwargs.get("tenant") in TRACED_TENANTS

wrapture.binding(PaymentGateway, "charge", when=traced_tenant).apply()

# later, in a live process: start tracing one tenant, then stop
TRACED_TENANTS.add("acme")
TRACED_TENANTS.discard("acme")
```

Two cautions. The predicate runs in the application's call path on
every operation while recording is active, so keep it fast. And it is
ordinary user code: if it raises, the caller sees the exception, the
same contract as a custom capture policy.

Per-operation sampling fits here too
(`when=lambda instance, args, kwargs: random.random() < 0.01`), but
where nesting matters prefer `Sample`, which decides at the root and
keeps whole trees together.

## Streaming to disk: JSONLines

`JSONLines(path)` writes each completed event to a file as one JSON
object per line, the [JSON Lines](https://jsonlines.org/) format that
`jq`, pandas and log tooling consume directly. A line is written when
an event closes, exit and error alike, so every line carries the
outcome and the timing; lines therefore appear in completion order,
children before the operation that contains them, and sorting by
`seq` with nesting rebuilt from `parent_id` recovers the tree.

The form is deliberately stable. Every line has `seq`, `parent_id`
(null for a root), `depth`, `kind`, `path`, and the `thread_id` and
`thread_name` of where the operation began; everything else appears
only when it was observed: `label`, `started` and `duration`
(plus `body_duration` and `items` for generators), `arguments` or the
raw `args`/`kwargs` shape, `forwarded`, `result`, `exception` (type
and message), `value` and `previous` for attribute writes, `injected`,
`stack`, and `data`. Absence means "not captured", so an absent
`result` stays distinguishable from `"result": null`, a call that
returned None, exactly the distinction `MISSING` preserves in memory.

Two properties make it safe to leave running:

- **The application is never blocked on I/O.** Lines go onto a
  bounded queue drained by a background writer thread; when the queue
  is full the line is dropped and counted on `dropped` rather than
  making the observed call wait. `flush()` blocks briefly until
  queued lines are on disk (`shutdown()` and `flush_sinks()`
  call it for you); `close()` flushes, stops the writer, and closes
  the file.
- **It declares `"summary"` capture**, so it neither retains live
  objects nor fails on unserialisable ones, and values that reach it
  captured by reference anyway (a binding's override, a tape's
  requirement) are reduced to bounded summaries at serialisation
  time, with a depth limit that cuts even self-referential
  structures.

There are no built-in size caps; growth is managed by putting a time
variable in the path and rotating on an interval, described next.

## Output paths and rotation

Every path wrapture writes to, a JSONLines trace or a Printer file, is
a template, expanded when the file is opened and never per line. A
path with no variables is a template that expands to itself. The
variables:

| variable | value |
|---|---|
| `{pid}` | process id, for pre-fork workers writing side by side |
| `{host}` | hostname, for fleets writing to shared storage |
| `{name}` | the sink's `name=`, defaulting to its type (`jsonlines`, `printer`) |
| `{date}` | `2026-08-18`, local time |
| `{time}` | `14-05-30`, local time, filesystem-safe |
| `{datetime}` | `2026-08-18T14-05-30` |
| `{epoch}` | `1755525930`, integer seconds |
| `{now:%Y%m%d-%H}` | any `strftime` format, local time |
| `{utc:%Y%m%d-%H}` | any `strftime` format, UTC |

A misspelt variable is an error where the path is given, not an hour
into a run when the file first rotates. Values are sanitised so an
expansion can never introduce a path separator or climb out of the
directory the template's static part names, and the parent
directories of the expanded path are always created, so
`traces/{date}/trace-{time}.jsonl` simply works. `sink.path` says
which file the sink is writing right now, and the sink's `repr()`
(which `config.report()` includes) shows both the template and the
current file.

Rotation is wrapture's own. `reopen()` closes the current file,
expands the template again and opens whatever that names, so with a
time variable in the path it moves on to a new file, on every
platform, with no external mover; queued JSONLines lines drain to the
old file first. `rotate=` calls it on an interval (`"15m"`, `"1h"`,
`"1d"`, `"1h30m"`, or a number of seconds), and `align=True` puts
that on the wall-clock boundary in local time, so hourly means on the
hour and daily at midnight, computed afresh each time so daylight
saving is followed rather than drifted through. A path with no time
variable and a `rotate=` reopens the same file each time, which is
pointless, and is warned about when the sink is built. For rotation
on demand, call `reopen()` from a signal handler.

```python
wrapture.add_sink(
    wrapture.JSONLines("traces/{date}/trace-{time}-{pid}.jsonl", rotate="1h", align=True)
)
```

The interval timer is a single daemon thread per process, started by
the first sink that needs it, so a sink with no `rotate=` costs no
thread.

This completes the inherited-dev-server story from the top of this
page. The whole intervention is a few lines in the application's entry
point, with no timeline and no changes to the code being observed:

```python
# the tail of manage.py, or the app factory
import wrapture
from myapp.orders import OrderService
from myapp.payments import PaymentGateway

wrapture.binding(OrderService, "place_order").apply()
wrapture.binding(OrderService, "restock").apply()
wrapture.binding(PaymentGateway, "charge").apply()

wrapture.add_sink(wrapture.Fanout(
    wrapture.Depth(2, wrapture.Printer()),
    wrapture.JSONLines("trace.jsonl"),
))
```

Requests then print a live two-level tree to the console while the
full nested trace streams to `trace.jsonl`, worker threads included,
ready for `jq`:

```console
$ jq -c 'select(.path | endswith("PaymentGateway.charge"))' trace.jsonl
$ jq -c 'select(.exception)' trace.jsonl
```

## Writing your own sink

The protocol is small enough that special-purpose sinks are cheap to
write. A sink that remembers which operations ran longer than a
threshold, keeping numbers rather than events:

```python
class SlowCalls(wrapture.Sink):
    """Remember the operations that ran longer than a threshold."""

    capture_args = "none"
    capture_result = "none"

    def __init__(self, threshold):
        self.threshold = threshold
        self.slow = []

    def on_exit(self, event):
        if event.duration is not None and event.duration > self.threshold:
            self.slow.append((event.path, event.duration))
```

Declaring `"none"` on both axes keeps it near-free, exactly as with
the counting sinks above; drop the declarations if the sink needs to
look at argument or result values.

One caveat for sinks that hold state, like the dictionary above:
notifications arrive from whatever thread ran the observed operation,
so a sink shared across threads must protect its own state if it
mutates anything more compound than the example's per-key counter.

## Instrumenting your own code

wrapture's config route exists for code you do not own: the wrapping
is declared outside, the observed code never imports wrapture, and
nothing needs to be redeployed differently. For your own code the
calculus flips. An author happy to mark up their source finds a
separate config file friction, because the declaration lives apart
from the code it describes; for them wrapture has an embedded
surface, four pieces that read as one:

- **`@wrapture.observed`** is the def-site decorator: the function's
  calls record as `"call"` events wherever it ends up, near-free when
  nothing listens. Bare on a def, or
  `@wrapture.observed(label="charge", capture="summary")` with
  options; everything but the callable is keyword-only. The
  [unit testing guide](unit-testing.md#observing-a-bare-callable)
  covers the proxy's full character, including the callables no
  binding could reach anyway (closures, partials, callbacks).
- **`wrapture.block(name, **data)`** marks what is smaller than a
  function: a with statement declaring the enclosed stretch as one
  `"block"` event, children nesting under it, the body's wall time as
  its duration, an escaping exception recorded and still propagated.
  Like a log statement, it is inert when nothing listens, so the
  marker stays in production code permanently.
- **`wrapture.annotate(**data)`** attaches what the code knows and no
  generic capture can infer (a row count, a cache hit) to the
  in-flight event, and is unconditionally safe to call. The number an
  aggregating sink wants is an annotation on an event: count inside
  the loop, annotate the total onto the enclosing block on the way
  out, one event rather than ten thousand.
- **`WSGIMiddleware` and `ASGIMiddleware`** wrap the application
  object at the edge in code,
  `application = wrapture.WSGIMiddleware(application)`, grouping
  events per request exactly as the config-declared `mode = "wsgi"`
  binding does, since both are the same class.

Sinks complete the all-in-code deployment: a CLI tool or worker that
configures its observability from command line options calls
`add_sink()` programmatically and never touches a config file.
Tracing needs nothing at all, since it is on by default: a root
block or observed call mints a trace identity, and `trace_headers()`
carries it outward. Deliberately, no code surface can force a trace
on: the `[trace]` table's `enabled = false` is an operator's
decision, and the per-entry `trace = true` exception lives in the
same file, written by the same hand. Code that wants its blocks
traced simply leaves tracing enabled.

The two postures are a choice, not a default and a workaround, and
they mix: a service can decorate its own critical paths and still be
swept by an operator's config, the events landing on the same tape
and the same sinks either way.

## Configuring from a file

The dev-server example above still edits the application's entry
point. A config file moves the same setup out of the code entirely:
observe rules, the sink, capture and sampling, written as TOML and
applied as one unit. This is the format the `python -m wrapture`
runner below consumes, the autowrapt injection path (arriving with
the injection layer) will consume, and code can apply directly:

```python
import wrapture

source = wrapture.find_config()
if source is not None:
    wrapture.load_config(source).apply()
```

`find_config()` implements the source precedence: a path in the
`WRAPTURE_CONFIG` environment variable, then `wrapture.toml` in the
current directory, then a `[tool.wrapture]` table in
`pyproject.toml`. The first source found wins outright; sources are
never merged.

A complete file:

```toml
pythonpath = "tracing"
capture = "summary"

[[observe]]
target = "myapp.orders:OrderService"
match = "*"
exclude = "_*"

[[observe]]
target = "myapp.payments:PaymentGateway"
name = ["charge", "refund"]

[[observe]]
target = "myapp.parsers"
match = "parse_*"

[[sink]]
type = "jsonlines"
path = "trace.jsonl"

[[setup]]
module = "myapp.orders"
call = "wrapture_local.hooks:instrument_orders"
```

### Choosing what to observe

Each `[[observe]]` entry binds members of one exact target. `target`
is never a pattern: it is `"module"` or `"module:path"`, the same
colon convention event paths use, so a bound member's event path is
literally the target plus the member's name. Members within the
target come from `name` or `match`, one of which is required; each
accepts a single string or a list:

- `name` lists exact members. Every listed name must exist or the
  config fails to apply, and it binds anything, properties and other
  attributes included.
- `match` is an `fnmatchcase` pattern over the target's own immediate
  members only: no traversal into nested classes or submodules, and
  no inherited members. It selects routines only (functions, static
  and class methods; for a module target, only functions the module
  itself defines), skipping properties, nested classes, plain data
  and anything already wrapped; `name` is the escape hatch that binds
  the skipped kinds explicitly. `exclude` subtracts patterns from the
  match, and a match that selects nothing warns with
  `ConfigWarning`.
- `mode` is normally omitted, leaving each binding to detect its own.
  The accepted values are `"wsgi"` and `"asgi"`, which wrap the named
  members as applications of that protocol in the recording
  middleware; each requires `name`, since a pattern must never
  bulk-install middleware, and for a wsgi or asgi entry `redact`
  names query string parameters. The
  [WSGI](wsgi-tracing.md) and [ASGI](asgi-tracing.md) request
  tracing pages cover them.

The blast radius of a pattern is thereby one level of one named
container, stated on the line above it.

The same selection is available in code as `discover()`, which takes
the target and patterns an observe entry would and returns a binding
group; the two share one implementation, so a pattern selects the
same members however it is spelt. Unlike an observe entry it resolves
immediately and an empty selection raises; the
[unit testing page](unit-testing.md) covers it.

Observe entries defer. Applying a config never imports a target
module: it registers a post-import hook per target, which fires
immediately when the module is already imported and otherwise when
the application imports it, at which point the entry's members are
resolved and its bindings applied. The application's import order is
never changed by observing it. This holds under Python 3.15's lazy
imports too: a `lazy import` statement imports nothing, and first
use reifies the module through the normal import machinery, so the
bindings land at that moment, before the touched attribute is even
fetched. Deferral has one honest cost: a
misspelled target module is indistinguishable from one not imported
yet, so it is not an apply-time error; the applied record's
`pending` view names the entries still waiting, and a target whose
module is never imported over the life of the process is reported in
a `ConfigWarning` at interpreter shutdown, so an empty trace has an
explanation.

The top-level `capture` key overrides the capture level on every
binding the file creates.

`redact` on an entry names parameters whose values are replaced with
`<redacted>` on that entry's bindings, everything else capturing at
the configured level. This is the expected posture for anything
leaving the process: streaming sinks already reduce values to
bounded summaries, but a summary of a secret is still a secret, so
tokens, card numbers and credentials should be redacted by name at
the entry, where they never reach any sink at all.

### Sinks: the [[sink]] list

`[[sink]]` is a list of destinations; several entries fan out
implicitly, so the list is the fanout and a single entry is a list
of one. Each entry names its sink with `type` and passes every other
key to the constructor. Two builtin names cover the event streams,
`printer` and `jsonlines`; anything else is reached by `module:attr`
reference: a callable, invoked with the remaining keys as keyword
arguments, that must return a `Sink`. So everything to a daily
rotating trace plus a shallow live view is two entries:

```toml
[[sink]]
type = "jsonlines"
path = "traces/trace-{date}-{pid}.jsonl"
rotate = "1d"
align = true

[[sink]]
type = "printer"
depth = 2
```

The `depth` key above is one of three gating keys an entry can
carry, each the file's spelling of a combinator wrapped around that
sink alone: `sample = 0.1` keeps that fraction of whole call trees
(`Sample`), `depth = 2` forwards only the top levels of each tree
(`Depth`), and `filter` forwards only matching events (`Filter`).
`filter` is either a table of event fields to patterns, `kind`,
`path` and `label`, each a string or list of strings matched with
`fnmatch` rules and all of which must match, or a `module:attr`
reference to a predicate taking the event. Several gating keys on
one entry wrap in a fixed order however they are written, sample
outermost, then depth, then filter innermost:

```toml
[[sink]]
type = "printer"
sample = 0.5
depth = 1
filter = { kind = "request" }

[[sink]]
type = "jsonlines"
path = "traces/orders-{date}.jsonl"
filter = { path = "myapp.orders:*" }
```

There is no top-level sampling key: sampling belongs to the sink it
gates, and a whole-config rate is one `sample` on each entry.

Sinks in `[[sink]]` listen for the life of the process. For sinks
that should listen only for a while, on a schedule or on demand, and
for collectors that produce periodic reports, see
[Windows](scheduled-tracing.md), whose `[[window.collect]]` entries use this
same grammar.

One nesting case exists, for a factory sink that is a container in
its own right, routing or fanning out internally: it takes the
sinks it routes to under `to`, written as `[[sink.to]]` entries and
passed to the factory as a `to=` list of built sinks. Inner entries
use exactly the same grammar, gating keys included, so nothing
deepens further; anything more elaborate belongs in Python:

```toml
[[sink]]
type = "wrapture_local.sinks:Router"

[[sink.to]]
type = "jsonlines"
path = "traces/requests-{date}.jsonl"
filter = { kind = "request" }

[[sink.to]]
type = "printer"
depth = 1
```

```python
# wrapture_local/sinks.py
import wrapture

class Router(wrapture.Sink):
    def __init__(self, to):
        self.to = to
    ...
```

Sink references resolve when the file loads, because the sink must
exist before events can flow. A relative `path` on a builtin sink is
anchored to the config file's own directory, as `pythonpath` entries
are, so the file says where its output goes whatever the process's
working directory; a factory receives its keys as written.

### Capturing log messages: the [[log]] list

Each `[[log]]` entry is `capture_logs()` spelt as TOML: the log
messages the application emits through the standard library logging
module become events of kind `"log"`, flowing to the config's sinks
beside the calls, nested under whatever observed operation emitted
them. The keys are the function's arguments, all optional:

```toml
[[log]]
name = "myapp.*"
level = "INFO"
exclude = "myapp.health*"
exclude_message = "*password*"
```

`name` and `exclude` are fnmatch patterns (or lists) over logger
names, and `level` is a threshold, name or number, meaning "at least
this severe"; the default WARNING keeps volume deliberate. Repeat the
entry for different patterns at different levels. Unlike an observe
entry there is no target to wait for, so captures apply immediately
when the config does, and `report()` lists them.

Capture hears each record once, on the logger that emitted it,
before propagation and regardless of the application's handler
configuration, which it never touches; records suppressed by a
logger's own level were never emitted and are not captured.
`exclude_message` is the safety valve, dropping matching messages at
capture so they reach no sink and no file, for content that must
never be recorded anywhere. Selection any finer belongs to the
sinks: a `filter` predicate on one `[[sink]]` sees the whole event,
`data["message"]` included, so routing (say, structured messages to
one file) is a predicate function away without blinding the other
sinks.

The recorded fields ride in `event.data` (`level`, `levelno`,
`message`, `module`, `funcName`, `lineno`), and a record logged with
`exc_info` carries its exception on the event's `exception` field,
never in the message: the message-plus-traceback blob is a Formatter
artifact of the logging module's own output path, and wrapture
captures the record, not the output. One retention note for
long-lived processes: a live exception keeps its traceback frames
for the event's lifetime, so `logging.exception()` in a hot loop is
a heavier capture than the message alone.

### Setup callbacks: running code at apply time

Declarative `[[observe]]` entries cover plain observation. Everything
richer (behaviour pipelines, redaction policies, `when=` predicates,
iterator proxies) lives in ordinary Python that a `[[setup]]` entry
triggers at the right moment: `call` names a `module:attr` callable
invoked with the module named by `module` as soon as that module is
imported, or immediately if it already was.

Any other key on the entry rides through to the handler as a keyword
argument, the same convention a `[[sink]]` entry uses, so one
generic handler can be specialised per deployment from the file:

```toml
[[setup]]
module = "myapp.orders"
call = "wrapture_local.hooks:instrument_orders"
tenants = ["acme", "globex"]
```

```python
# wrapture_local/hooks.py
import wrapture

def instrument_orders(module, *, tenants=()):
    def traced_tenant(instance, args, kwargs):
        return kwargs.get("tenant") in tenants

    wrapture.binding(module.OrderService, "restock",
                     when=traced_tenant).apply()
```

With no extra keys the handler is called with just the module, so a
plain `handler(module)` signature keeps working; option values are
limited to what TOML can express, and a handler rejecting an option
(a typo, say) follows the usual failure posture below. Unlike sink
references, the handler reference resolves only when the hook fires:
by then the trigger module is mid-import anyway, so naming operator
code here can never cause it to be imported ahead of the module it
instruments.

A package can also ship a whole family of handlers (instrumentation
for every interesting module of a framework, say) and have the
config activate it with one entry. The package declares the family
as entry points in its own metadata, entry name the trigger module
and target the handler, the same shape wrapt's own hook discovery
reads:

```toml
# the wrapture-flask package's pyproject.toml
[project.entry-points.wrapture_flask]
"flask.app" = "wrapture_flask.hooks:instrument_app"
"flask.blueprints" = "wrapture_flask.hooks:instrument_blueprints"
```

The config names the group instead of a module and call (the two
forms are mutually exclusive), and any extra keys go to every
handler in the family:

```toml
[[setup]]
group = "wrapture_flask"
capture_headers = false
```

Discovery reads metadata alone at apply time, importing nothing, and
each handler still resolves only when its own module arrives. A
group with no entry points is a loud `ConfigError`, since it means a
misspelled name or an uninstalled package. Because the entry point
shape is exactly wrapt's convention, a family whose handlers default
every option is equally usable through raw
`wrapt.discover_post_import_hooks()`; the config route adds the
options, the failure posture, and the config file's control over
when it all happens.

### Code next to the config file

Uninstalled operator code, the factories and callbacks above, is made
importable by the top-level `pythonpath` key: a directory or list of
directories prepended to `sys.path` when the file loads, with
relative entries anchored to the config file's own directory, so a
config plus its companion code stays a self-contained bundle wherever
the process launches from. Prepending can shadow installed modules,
so keep such code in a distinctively named package (`wrapture_local/`
above), never in loose generically named files.

### Trust and failure: what a bad config does

A config file can name arbitrary code to run, so loading one is
equivalent to executing code; the trust boundary is write access to
the file, as it already is for anything else the process imports.
Failures are loud: unknown keys, `name` and `match` together, a
reference that does not resolve, a factory that returns something
other than a `Sink`, all raise `ConfigError`, and a config that
fails partway through applying unwinds whatever it had installed
before the error propagates. A `Tape` is refused as the sink,
however deeply a factory's composition buries one: a tape retains
every event and a config sink lives for the life of the process, so
unbounded retention stays a deliberate code-level choice.

Validation that needs the target module (a `name` that must exist, a
`match` with nothing to select) runs when the entry's hook fires,
and the posture depends on when that is. Fired during apply itself
(the module was already imported), the failure is the caller's to
hear and raises. Fired later, from inside the application's own
import of the module, it warns with `ConfigWarning` and drops the
entry instead, because observation must never fail the import it
rode in on; a setup callback that raises after apply is handled the
same way.

The same setup is available programmatically as the `Config` class
with `ObserveEntry` and `SetupEntry` values, which is exactly what
the loader builds: the file can say nothing that `Config` cannot,
and code can additionally pass live objects, a constructed sink or a
callable capture policy, where the file is limited to what TOML can
spell. `apply()` returns the live `AppliedConfig` record: `bindings`
grows as hooks fire, `pending` names the entries still waiting,
`report()` renders the whole picture as text, which is the way to
ask an injected process what is actually installed, and `revert()`
takes everything down again, neutralising hooks that have not fired.

### Zero-code runs: python -m wrapture

The runner applies a config and then runs your program, with nothing
in the program saying so:

```console
$ python -m wrapture -m myapp
$ python -m wrapture manage.py runserver
$ python -m wrapture --config trace.toml -m myapp
```

Without `--config` the precedence chain above locates the file, and
finding none is an error rather than a silent untraced run. The
target runs as `__main__` with `sys.argv` rebuilt to the target and
its own arguments, exactly as `python -m myapp` or
`python manage.py runserver` would have run it, the same `-m`
convention as pdb, cProfile and coverage. Everything after the
target belongs to the target, however option-like it looks, so
wrapture's options go before it.

The ordering is the point: the config is applied before the target
runs, so patches are in place before the target module imports
anything, and a `from applib import parse` in the application still
picks up the observed function. This is the zero-code form of the
dev-server scenario at the top of this page: the few lines in the
entry point become a `wrapture.toml` next to the project, and
`python -m wrapture manage.py runserver` traces the inherited
application untouched.

Runnable demonstrations of this whole workflow live in the
[examples directory](https://github.com/GrahamDumpleton/wrapture/tree/main/examples)
of the repository: a live printer over an order flow, a threaded
pipeline streamed to disk and rendered in Perfetto, and an
operator-code bundle showing `pythonpath`, a setup callback and a
sink factory together. Each is a directory to `cd` into and run.

## Injection without a launcher: autowrapt

The runner still owns the command line. When even that is
unavailable, because something else launches the process (a service
manager, a container entry point, a WSGI server), the same config
can be injected at interpreter startup through
[autowrapt](https://github.com/GrahamDumpleton/autowrapt). Two
opt-ins gate it, both outside wrapture:

```console
$ pip install autowrapt
$ AUTOWRAPT_BOOTSTRAP=wrapture python myapp.py
```

Installing autowrapt is what makes interpreter startup do anything
at all, and the environment variable names an entry point group that
autowrapt hands to wrapt's post-import hook discovery once site
initialisation completes; wrapture's entry in that group hooks a
module that is always already imported, so its bootstrap fires
immediately, at startup. Absent either opt-in, the entry point in
wrapture's package metadata is inert: wrapture has no dependency on
autowrapt and never imports it. When it fires, the bootstrap
resolves the same config precedence chain the runner uses and
applies what it finds, before the application's own code runs. The
runner and autowrapt are two doorways into identical machinery;
nothing is expressible through one and not the other.

Positioning matters here. Injection is a development, staging and
break-glass tool: the unwritten rule for autowrapt is that it is not
installed on production systems in normal situations, precisely
because of what it enables, and that installation gate is the
feature. Production tracing is the code-level path from earlier on
this page, an application registering its own process sink at
startup. Two operational cautions follow from the mechanism:

- Injection never takes the process down. A missing config warns
  (`ConfigWarning`) and the process starts untraced; a config that
  exists but cannot be applied warns the same way, because an error
  raised at bootstrap is fatal to an interpreter that has not even
  started, and the environment variable reaches every Python process
  launched under it, not just the one you meant. The loud failures
  belong to the runner and the programmatic path, where the caller
  chose to apply.
- Observe entries defer, so the bootstrap imports no application
  code: bindings land as the application imports its own modules, in
  its own order, and a Django models module is observed the moment
  Django itself brings it in. What does resolve at bootstrap is the
  `[[sink]]` references, because a sink must exist before events flow;
  operator code it lives in must be reachable then, which is what
  `pythonpath` is for.
- The variable's reach is configurable. `inherit = false` in the
  config strips wrapture's own name from `AUTOWRAPT_BOOTSTRAP` once
  the config applies, so Python processes the application launches
  by exec or spawn start untraced; other tools named on the variable
  are untouched, and `WRAPTURE_CONFIG` is left alone. The default
  inherits, because launched workers (a dev server's autoreloader
  child, spawned pool workers) are usually the application itself.
  Forked workers are outside either setting: a fork inherits the
  parent's memory, patches and sinks included, without re-running
  interpreter startup.

Once injected, the process is still operable. The bootstrap keeps
its `AppliedConfig` record on `wrapture.bootstrap.applied`, since
nothing else in an injected process holds it: from a console, a
debugger or a signal handler, `report()` lists what is installed and
what is still pending, `suspend()` and `resume()` are the runtime
toggle (wrappers stay in place, operations pass through and are
counted, and a pending entry firing while suspended arrives
suspended too), and `revert()` takes the whole intervention down
without restarting the application.

## Trace identity and propagation

wrapture's event linkage, sequence numbers and parent ids, is
process local. A **trace identity** extends the tree across
processes: every tree of events carries a distributed trace id, the
W3C trace context, and the identity travels in the `traceparent`
header of outbound requests, so two services both observed by
wrapture join their trace files on one id, with or without any
tracing backend involved.

**Every tree rooted in an operation is a trace.** The mechanism is
on by default: a root call, request or block event that inherited no
context mints a fresh identity, one random id per tree, only while
events are being recorded at all, so the cost when nothing listens
stays nothing. Traces start at declared operation boundaries, a
function invoked, a request arriving or a block entered, not at
accesses or messages: a tree rooted in an attribute event carries no
identity, and a log message never starts a trace, though one
recorded inside a traced tree shares its identity like any other
child. Children share their tree's context, readable in code as
`event.trace`, and every JSONLines line carries the identity under
its `trace` key:

```json
{"seq": 3, "path": "backend:quote", "trace": {"w3c": {"trace_id": "1f4c24c6b0e14f6a9d2c8e5a7b3f9d10", "sampled": true}}}
```

The [WSGI](wsgi-tracing.md) and [ASGI](asgi-tracing.md) middleware
are the special case at the ingress boundary: an arriving request
carrying a `traceparent` header joins the caller's distributed trace
rather than minting, and its `tracestate` rides along untouched. A
request with no recognised headers mints as any root does. A
boundary inside an already-traced tree keeps the enclosing identity
unless it receives headers of its own, which start a fresh scope for
its subtree.

Processes with no HTTP ingress get their trace root from a block: a
cron job, CLI command or queue worker wraps its operation in
`with wrapture.block("process-batch"):` and the root block mints,
`trace_headers()` supplying the identity for whatever it sends
outward. The block's name can even be keyed dynamically
(`block(f"deploy {target}")`) where a function name is fixed, which
is why blocks mint rather than deferring to an `@observed` function
at the top; a request with incoming headers nested under such a root
still joins its own trace, since ingress parsing at the boundary is
independent of root minting.

On the way out, instrumentation injects the identity into outbound
traffic. The whole public surface such a probe needs is two
functions. `wrapture.current_trace()` answers "what trace is this
operation part of", carrier-agnostic: a probe for a transport with
no header concept (trace context in a SQL comment, a database
session variable) reads the slot's ids off it and renders them its
own way. `wrapture.trace_headers()` is the convenience for any
carrier of named values, HTTP request headers foremost but equally
message-queue headers or gRPC metadata: it returns the pairs an
outbound message made right now should carry, empty when nothing is
being recorded, so injection is always safe to attempt. A probe is a
setup hook plus one behaviour stage; the trace-propagation example's
`urllib_support.py` is the complete pattern, a client and a server
in two processes sharing ids through nothing but the header.

W3C trace context is the one wire format wrapture speaks. The
ecosystem has converged on it, and vendor baggage rides inside the
standard's own `tracestate` header, so there is nothing left for a
second convention to carry. One invariant governs an identity
wrapture parses but nothing claims: **never break a trace you do
not understand**. An arriving identity keeps its raw headers on the
slot, and on egress a slot no tracing sink has claimed forwards
those headers verbatim, so the upstream product sees this service
as a transparent hop and its trace stays connected, while a claimed
slot is rewritten with span ids that really get exported. Headers
wrapture does not parse are never touched at all: a request
carrying a vendor format alongside `traceparent` (services
mid-migration send both) flows through the application with no
wrapture involvement in the vendor headers whatsoever.

Configuration is the top-level `[trace]` table, and the noun is
deliberate: this switches trace *identities*, never observation,
recording or sinks:

```toml
[trace]
enabled = false        # ids and propagation off process-wide

[[observe]]
target = "myapp.jobs"
match = "run_*"
trace = true           # these roots mint identities anyway
```

`trace = true` on an observe entry is the case-by-case re-enable
under a global disable: that entry's roots, a background job wanting
an identity for its outbound calls, say, mint even while the
mechanism is off elsewhere. Because only operations mint, the flag
lands on the entry's call and request bindings; an entry that binds
nothing but attributes is rejected, since the flag could never act
there. With no `[trace]` table at all, the default stands: enabled.

## Exporting traces to other tools

Three exporters render a trace for existing tools rather than a
viewer of wrapture's own. Each accepts either a `Tape` or event
records in the serialised JSONLines form, so a trace can be exported
live inside a test or long after the fact from a file the runner
produced; `load_events(path)` reads such a file back. From a shell,
`python -m wrapture.tools convert` does the same without writing any
code (bare `python -m wrapture.tools` lists the available commands).
A tracing backend with a span model of its own is fed live by a sink
instead; the OpenTelemetry bridge closing this section is the worked
case.

### A timeline in Perfetto: chrome_trace

`chrome_trace()` renders the trace in Chrome trace JSON, the format
the [Perfetto UI](https://ui.perfetto.dev) (and the older
`chrome://tracing`) opens directly:

```console
$ python -m wrapture.tools convert --format chrome -o trace.json trace.jsonl
```

Drop `trace.json` onto Perfetto and the trace becomes a navigable
timeline: one lane per thread, one slice per event, nested slices
for nested events, with widths proportional to duration. Clicking a
slice shows the captured arguments, result or exception in the
detail pane. The gaps between slices are unobserved time, which for
a deliberately sparse trace is itself information. An event that
never closed appears as a begin with no end, and a generator's slice
spans creation to close with its accumulated body time alongside.

### Architectural snapshots: canonical

`canonical()` renders the call tree as a deterministic text
fingerprint: kind and path per line, indented by nesting, `!!` with
the exception type for failures, `(injected)` for outcomes supplied
by behaviour. Everything unstable between runs (sequence numbers,
timings, captured values, thread identity) is left out:

```python
with wrapture.timeline(place, charge, ledger) as tape:
    checkout(order)

assert wrapture.canonical(tape) == snapshot
```

Snapshot it once (a golden file, or a tool such as syrupy) and a
refactor that silently changes what calls what fails the comparison
as a diff a reviewer can read. This is the complement to
`assert_order`: the assertion states the rules you thought of, the
snapshot catches the changes you did not. From a shell,
`convert --format canonical` renders a trace file the same way.

### Diagrams where Mermaid renders: mermaid

`mermaid()` renders the trace as a sequence diagram: participants
are the classes and modules, messages are the members called on them
in recorded order, failures return their exception type, and
attribute events carry their kind (`status (set)`). Mermaid renders
natively on GitHub and in most documentation tooling, so the output
pastes straight into a pull request comment;
`convert --format mermaid` writes it to standard output ready for
the clipboard. Best kept to small traces; sequence diagrams stop
being readable beyond a few dozen events.

```text
sequenceDiagram
    participant caller
    participant P1 as OrderService
    participant P2 as PaymentGateway
    caller->>+P1: place_order
    P1->>+P2: charge
    P2-->>-P1: TimeoutError
    P1-->>-caller: return
```

### Live traces in OpenTelemetry: a sink

The converters above render a trace after the fact, from a tape or a
file. Feeding a tracing backend while the application runs is a job
for a sink, and OpenTelemetry's is one wrapture ships: the
`wrapture.otel` subpackage exports request trees over OTLP to
whatever backend the standard OTel environment variables name, with
the OpenTelemetry dependencies behind the `wrapture[otel]` extra.
It is also the worked case for
[a sink of your own](#writing-your-own-sink) feeding any backend
with a span model; the pattern is the same, only the API names
change.

Most of the mapping writes itself, because the two models are close:

- One event becomes one span, opened at `on_enter` and closed at
  exactly one of `on_exit` or `on_error`, the error close recording
  the event's exception on the span and marking its status.
- `"request"` events become SERVER spans named access-log style
  (`GET /quote/widget`), carrying the method, path and status code
  under their semantic-convention attribute names; `"call"` events
  become INTERNAL spans named by the binding, and `"block"` events
  become INTERNAL spans named by the block, which is how an embedded
  `with wrapture.block("render-invoice"):` shows up as a span with
  no OTel API in the code. Attribute events are skipped as too
  fine-grained for a trace, the same judgement the `kind` filter
  spells in a config file.
- Captured arguments, results and `annotate()` data become span
  attributes, flattened onto dotted names (`wrapture.arg.item`,
  `wrapture.data.rows`) because OTel attributes hold only scalars and
  lists of scalars. The data dict is read again when the event
  closes, since annotations merge in while the operation runs. The
  sink declares `"summary"` capture on both axes, so the values
  reaching it are already bounded text and scalars, safe to hand to
  an exporter and impossible to retain live objects through.
- `flush()` forwards to the provider's `force_flush()`, so the flush
  wrapture gives process sinks at interpreter exit also drains OTel's
  batch processor, and the tail of a trace is not lost in its buffer.

Two moves are less obvious, and they are the part worth copying into
a bridge to any other backend. The first is parenting. OTel normally
parents spans through ambient context, but sink notifications arrive
on whatever thread ran the observed operation, and a generator or a
streamed response body can close on a different thread than it
started on, so the ambient context at the close is not reliably the
one from the start. The event's `parent_id` is correct in all of
those cases, so the sink parents explicitly through a map of open
spans:

```python
def on_enter(self, event):
    context = None
    if event.parent_id is not None:
        with self._lock:
            entry = self._spans.get(event.parent_id)
        if entry is not None:
            context = trace.set_span_in_context(entry[0])

    span = self._tracer.start_span(
        name=event.label or event.path,
        context=context,
        start_time=self._to_epoch_ns(event.started),
        ...
    )

    with self._lock:
        self._spans[event.seq] = (span, time.monotonic())
```

The close pops the entry by `event.seq` and ends the span, so the map
holds only what is in flight. Children close before the operation
that contains them, which is what makes the lookup safe.

The second is time. Events are stamped on the `perf_counter` clock,
which has an arbitrary zero; OTel wants absolute epoch nanoseconds.
Sampling both clocks once, in the same breath at construction, pins
them together, and every event time then converts by one addition:

```python
self._epoch_offset_ns = time.time_ns() - int(time.perf_counter() * 1e9)

def _to_epoch_ns(self, perf_seconds):
    return self._epoch_offset_ns + int(perf_seconds * 1e9)
```

Passing `start_time` and `end_time` through this conversion means the
span timings are the event timings, not the moments the exporter
heard about them.

The sink also completes the
[trace identity](#trace-identity-and-propagation) story by claiming
the tree's w3c slot. There are two id generators in the
room, wrapture's minting (which must work with no OTel installed)
and the SDK's, and the sink makes them agree. For an identity that
arrived in headers, the root span is created with a remote parent
built from the slot, so the exported tree continues the caller's
trace rather than starting a detached one, and the arrived sampled
flag rides along for the SDK's parent-based sampler to honour: an
upstream "do not sample" exports nothing. For an identity wrapture
minted locally, the root span is created normally, the SDK
generating its own trace id, and the slot takes the whole identity
at that moment, before the operation's body runs, so serialised
files, outbound headers and exported spans all read one id and the
backend shows a clean native root. In both cases the slot's span-id
register then tracks the innermost exported span as spans open and
close, so `trace_headers()` carries a live parent at any moment
inside the tree, and downstream services attach to spans that
really got exported. When wrapture's own `sample =` gate drops a
tree, the sink never hears it: the minted id stands unclaimed and
outbound headers carry it, which is what "not sampled" means.

In a config file, OpenTelemetry export is first-class: the
top-level `[otel]` table, sibling to `[trace]`, is the one
registration covering every signal. Its presence opts in (export
needs an endpoint to be useful, so no table means no export), and
`enabled = false` is accepted so a stanza can be kept in the file
but switched off, matching the `[trace]` style. The `signals` key
says which signals are enabled, shared facts sit at the top of the
table, and each signal's own tuning nests beneath it:

```toml
[otel]
service_name = "flask-shop"
signals = ["traces", "metrics"]

[otel.traces]
sample = 0.1

[otel.metrics]
export_interval = 5

[otel.environment]
exporter_otlp_endpoint = "http://localhost:4318"
exporter_otlp_protocol = "http/protobuf"
```

The sink it builds always registers ahead of whatever the `[[sink]]`
list builds, which then stacks in file order as usual; a tracing
sink must hear a root event before any other sink can observe its
trace identity, and the table's position makes that ordering true by
construction rather than by convention. In code the same rule is the
caller's, and simply stated: add the OTel sink before other sinks.
The two neighbouring tables stay crisp: `[trace]` governs
identities, `[otel]` governs export. Any other export destination
remains a `[[sink]]` entry with a `module:attr` factory, which is
also the escape hatch spelling (`type = "wrapture.otel:sink"`) for
composing the OTel sink somewhere unusual, such as inside a window.

The factory composes what the table asks for from the pieces this
guide already covered: the span sink wrapped in `Sample` when
`traces.sample` is given, so the trace export is sampled per tree
while the metrics sink beside it still hears every event, the pair
delivered through a `Fanout`, whose capture negotiation means a
metrics-only registration stays at `"none"` while traces raise it to
`"summary"`.

`[otel.environment]` holds defaults for OTel's own environment
variables: each key is uppercased, prefixed with `OTEL_` when not
already, and applied with setdefault before the providers are
built, so the file can name any of the SDK's documented variables
without the factory knowing them individually, and a variable set in
the real environment always wins. That gives the file three
postures: self-contained (endpoint in the file, runs with no
environment setup), deployment-owned (no environment table, the real
environment decides everything), or mixed, defaults in the file with
the deployment overriding what differs. Named keys such as
`export_interval` are passed to constructors explicitly and beat
both spellings. The posture is wrapture-first: choosing wrapture
means taking all it does, including standing up the SDK providers,
which is what the zero-code story requires. An application that
already configured its own providers wins as the failsafe, the
telemetry flowing through the exporters the application chose, and
wrapture warns (a `ConfigWarning` per signal) naming what is lost:
the table's provider-level settings no longer apply, and behaviour
wrapture relies on, such as the sampler honouring an upstream
sampling decision, is whatever the application installed.
`OTEL_TRACES_EXPORTER=console` and
`OTEL_METRICS_EXPORTER=console` dump either signal to standard
output for a look without a collector; the examples README carries
the run commands:

```console
$ cd examples/flask-app
$ uv run --with flask --extra otel \
    python -m wrapture --config wrapture-otel.toml main.py
```

Each request then arrives in the backend as one trace: a SERVER root
span named for the request line, the view handler and its helpers
nested beneath it with their captured arguments and results, and the
failing request's tree marked as an error with the `KeyError`
recorded on the span that raised it.

The metrics signal aggregates the same events instead of exporting
them individually: request durations into the semantic-convention
`http.server.request.duration` histogram attributed by method and
status code, call durations into a per-path histogram whose error
series split out by exception type, and a counter of operations
observed beginning. The sink protocol needs nothing added for this;
the enter and close notifications are exactly the increment points
instruments want, and the design is the `Aggregate` collector's
(bounded memory, `"none"` capture on both axes, so nothing is ever
retained or even captured) with the aggregation handed to the OTel
SDK. The bound path is safe as a metric attribute precisely because
the config chose the bindings: the set of values is closed, where
the raw request URL is not, so requests are attributed by method and
status only. This pairing is also why `sample` lives under
`[otel.traces]` rather than as the registration's own gating key: a
gate on the whole registration would starve the histograms, while
sampling inside it drops only the span export, keeping the
always-on cheap signal complete beside the sampled drill-down.
