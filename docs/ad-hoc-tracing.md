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

## Writing a sink

The protocol is small enough that special-purpose sinks are cheap to
write. A sink that counts calls per path, retains nothing, and asks for
no values at all:

```python
class Counting(wrapture.Sink):
    """Count events per path; keep no values, no references."""

    capture_args = "none"
    capture_result = "none"

    def __init__(self):
        self.counts = {}

    def on_enter(self, event):
        self.counts[event.path] = self.counts.get(event.path, 0) + 1
```

Declaring `"none"` on both axes matters: if no other active sink asks
for more, recording skips value capture entirely, including signature
binding, which is the dominant cost of recording a call. A counting
sink over a hot method is therefore far cheaper than a recording tape
over the same method.

One caveat for sinks that hold state, like the dictionary above:
notifications arrive from whatever thread ran the observed operation,
so a sink shared across threads must protect its own state if it
mutates anything more compound than the example's per-key counter.
