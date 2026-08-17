# Ad-hoc tracing

The same bindings that drive tests can stream a live, structured trace
out of a running application. Nothing about a binding is test-specific:
it observes a call site and emits events, and what happens to those
events is decided by whoever is listening. This page covers that
listening side.

The tracing layer is being built out incrementally; this page documents
what exists so far and grows with it.

## Sinks

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

The first `add_sink()` also installs an atexit handler that calls
`flush()` on every process sink still registered at interpreter
shutdown, so the tail of a trace, usually the interesting part, is not
lost in a sink's buffers.

Some environments tear the interpreter down without ever running atexit
callbacks: embedded interpreters and subinterpreters are destroyed by
their host, and hosting platforms typically offer their own shutdown
notification instead. `flush_sinks()` performs exactly the operation
the atexit handler would, on demand, so it can be subscribed to
whatever the host provides. Under mod_wsgi, for example, subscribe it
to the process shutdown event, which fires while the interpreter and
its threads are still fully alive. Calling it more than once is safe,
and a sink that fails to flush is counted and skipped so the rest
still get their chance.

## Watching calls live: Printer

`Printer` is the simplest real sink: it prints each event as it
happens, one line when an operation begins, indented by nesting depth,
and a closing line with the outcome, using the same `->` and `!!`
markers as `tape.tree()`. It writes to the stream you give it, or to
`sys.stderr` by default, flushing every line so the trace is intact up
to the moment of a crash or hang.

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
Gateway.charge -> 'ch_500'
'ch_500'

>>> wrapture.remove_sink(printer)
>>> _ = charge.remove()

```

No timeline appears anywhere above: the binding is applied, a sink is
listening, and events flow. This is the minimal form of tracing a
running application: name the methods that matter in the application's
entry point, register a sink, and a nested trace appears as requests
come in, with no timeline and no changes to the code being observed.

`Printer` is the live view; `tape.tree()` is the tidy reconstruction
after the fact. In a test, both can run at once, since sinks compose:
keep the timeline for assertions and add a `Printer` while debugging.

## Counting without retaining

Not every question needs the events themselves; often a number is the
answer. Two sinks keep numbers and nothing else, so they are safe to
leave running for a whole test suite or a long-lived process:

- `Counter()` counts operations as they begin, failures included. One
  number, no retention.
- `Aggregate()` keeps one row per path: how many operations began, and
  the total, self, fastest and slowest execution times of the ones
  that completed, exceptions included. Memory is bounded by the number
  of bound locations, however many events flow.

Both declare `"none"` on both capture axes, which matters: when no
other active sink asks for more, recording skips value capture
entirely, including signature binding, the dominant cost of recording
a call. A counter over a hot method costs a fraction of what a
recording tape does.

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
without retaining a single event:

```python
hot = wrapture.add_sink(wrapture.Aggregate())
...
for path, row in sorted(
    hot.stats.items(), key=lambda item: item[1].self_total, reverse=True
):
    print(
        f"{path}: {row.count} calls,"
        f" {row.total:.3f}s total, {row.self_total:.3f}s self"
    )
```

`self_total` is the figure profilers rank by: the time spent in the
operation itself, excluding the time its observed children account
for. A method that is slow because of what it calls ranks low on self
time; a method that is slow in its own right ranks high, and that
distinction is what tells you where to look. No external profiler can
compute it for an arbitrary handful of bindings, because it only sees
whole call stacks; wrapture computes it from the parent links as
events close, retaining nothing.

Every recorded event carries `started` and `duration` (exceptions
included), which is what the rows are built from. For generators the
accumulated body time stands in for the wall duration, since the wall
figure includes the consumer's time between yields, which is not the
generator's to spend. The same numbers are available on a tape:
`tape.self_time(event)` for one event, and `tape.tree(times=True)`
for the whole picture, covered on the
[unit testing page](unit-testing.md).

## Composing sinks

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
  queued lines are on disk (the atexit handler and `flush_sinks()`
  call it for you); `close()` flushes, stops the writer, and closes
  the file.
- **It declares `"summary"` capture**, so it neither retains live
  objects nor fails on unserialisable ones, and values that reach it
  captured by reference anyway (a binding's override, a tape's
  requirement) are reduced to bounded summaries at serialisation
  time, with a depth limit that cuts even self-referential
  structures.

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

## Writing a sink

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
sample = 0.1

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

[sink]
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

The blast radius of a pattern is thereby one level of one named
container, stated on the line above it.

The top-level `capture` key overrides the capture level on every
binding the file creates, and `sample` keeps only that fraction of
call trees by wrapping the sink in `Sample`.

### The sink and its factory escape hatch

`[sink]` names the sink with `type` and passes every other key to its
constructor. Two builtin names cover the no-code cases: `printer` and
`jsonlines`. Anything else is reached by `module:attr` reference: a
callable, invoked with the remaining keys as keyword arguments, that
must return a `Sink`. The factory is also the composition escape
hatch; the file has no syntax for combinators because a factory can
return any arrangement:

```toml
[sink]
type = "wrapture_local.sinks:make_sink"
endpoint = "https://collector.internal"
```

```python
# wrapture_local/sinks.py
import wrapture

def make_sink(endpoint):
    return wrapture.Fanout(
        wrapture.Depth(2, wrapture.Printer()),
        MySender(endpoint),
    )
```

Sink references resolve when the file loads, because the sink must
exist before events can flow.

### Setup callbacks

Declarative `[[observe]]` entries cover plain observation. Everything
richer (behaviour pipelines, redaction policies, `when=` predicates,
iterator proxies) lives in ordinary Python that a `[[setup]]` entry
triggers at the right moment: `call` names a `module:attr` callable
invoked with the module named by `module` as soon as that module is
imported, or immediately if it already was.

```python
# wrapture_local/hooks.py
import wrapture

def instrument_orders(module):
    wrapture.binding(module.OrderService, "restock",
                     when=only_traced_tenants).apply()
```

Unlike sink references, the callback reference resolves only when the
hook fires: by then the trigger module is mid-import anyway, so
naming operator code here can never cause it to be imported ahead of
the module it instruments.

### Code next to the config file

Uninstalled operator code, the factories and callbacks above, is made
importable by the top-level `pythonpath` key: a directory or list of
directories prepended to `sys.path` when the file loads, with
relative entries anchored to the config file's own directory, so a
config plus its companion code stays a self-contained bundle wherever
the process launches from. Prepending can shadow installed modules,
so keep such code in a distinctively named package (`wrapture_local/`
above), never in loose generically named files.

### Trust and failure

A config file can name arbitrary code to run, so loading one is
equivalent to executing code; the trust boundary is write access to
the file, as it already is for anything else the process imports.
Failures are loud: unknown keys, `name` and `match` together, a named
member that does not exist, a reference that does not resolve, a
factory that returns something other than a `Sink`, all raise
`ConfigError`, and a config that fails partway through applying
unwinds whatever it had installed before the error propagates.

The same setup is available programmatically as the `Config` class
with `ObserveEntry` and `SetupEntry` values, which is exactly what
the loader builds: the file can say nothing that `Config` cannot,
and code can additionally pass live objects, a constructed sink or a
callable capture policy, where the file is limited to what TOML can
spell.

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

## Exporting traces

Three exporters render a trace for existing tools rather than a
viewer of wrapture's own. Each accepts either a `Tape` or event
records in the serialised JSONLines form, so a trace can be exported
live inside a test or long after the fact from a file the runner
produced; `load_events(path)` reads such a file back. From a shell,
`python -m wrapture.tools convert` does the same without writing any
code (bare `python -m wrapture.tools` lists the available commands).

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
