# Design concepts

wrapture is one mechanism worn three ways: monkey patching, unit
testing, and ad-hoc tracing. The guides go deep on each; this page is
the map to read first. It introduces every concept the guides build
on, a paragraph or two apiece, and shows how they compose, so that
when a guide says "the binding records onto the tape", both halves of
that sentence already mean something. Nothing here is exhaustive, and
everything links to the page that is.

## The wrapped call site

Everything starts from one move: wrap real code at a named location,
without the code knowing. The classes being observed do not import
wrapture, inherit from anything, or register themselves; wrapture
reaches them by name and installs a wrapper around the real callable,
using [wrapt](https://github.com/GrahamDumpleton/wrapt)'s
monkey-patching machinery. Unless told otherwise the wrapper is
transparent: the real code runs, with wrapture in a position to watch
the call, change it, or answer it. Removing the wrapper restores the
original exactly, and wrappers compose, so a site someone else has
already decorated or patched still works.

Wrapping rather than replacing is the load-bearing choice. Because the
real method still sits under the wrapper, calls an object makes to
itself pass through it too, an intervention can be surgical (rewrite
one argument, keep everything else real), and what gets recorded is
what the real code actually did. The
[design philosophy](design-philosophy.md) page explains the thinking;
here it is enough that every concept below sits on this one move.

## Bindings: naming a location

The central object is the **binding**. One binding names one location
and carries three separable things: *where* (the addressing), *what
should happen there* (the behaviour), and *whether it is currently
installed* (the lifecycle).

Addressing takes the forms code itself uses: a string module path
resolved late (`binding("myapp.gateway", "Gateway.charge")`), a class
or instance in hand (`binding(Gateway, "charge")`), an attribute slot
(`attr="TIMEOUT"`) or a mapping entry (`item="API_KEY"`). A misspelled
name fails on the line that made the mistake, not later as a patch
that never fired.

Declaration and effect are separate: `binding()` only declares, and
nothing changes until `apply()` installs the wrapper. `remove()`
restores the original, `suspend()` and `resume()` toggle it in place,
and a `with` block scopes apply and remove around a body. Several
bindings form a group with `bindings()`, one object with one
lifecycle that never half-applies. All of this is the
[monkey patching](monkey-patching.md) guide's subject.

A binding adapts its vocabulary to the shape of what it names. A
callable is wrapped around its calls; an attribute around its reads,
writes and deletes; a plain value or mapping entry is held rather
than wrapped; an iterable is wrapped around its consumption. The next
three sections take these in turn.

## Behaviour: channels, verbs and phases

What a binding does at its site is configured on **channels** named
for the interceptable operations: `on_call` for callables, and
`on_get`, `on_set`, `on_delete` for attributes. Each channel offers
two kinds of verb. **Stages** compose, each seeing the operation on
its way in or out: `transforms_args()` and `transforms_result()`
rewrite, `validates_args()` and `validates_result()` check without
changing. A **terminal** decides the outcome, and there is at most
one: `returns()` answers without running the real code, `raises()`
fails the call, `decorates()` takes full control in wrapt's wrapper
form, and `passes_through()` says explicitly that the real code runs.

A binding with no behaviour at all is the most common kind: it
observes. The real call runs and is recorded, and nothing else
happens. Stubbing, failing and transforming are opt-ins on top of
watching, not the baseline.

Behaviour can also be scripted to change over time. `then()` starts a
new **phase** after a count or a condition, with the full vocabulary
available in each phase, so "succeed twice, then time out" is three
lines rather than a hand-written counter. Phases are the tool for
behaviour that must change *within* one call of the code under test;
a test that sits between calls simply reconfigures the binding in
place.

## Value and mapping bindings: holding instead of wrapping

Not every patch intercepts an operation. A test often just needs an
environment variable set, a settings key changed, or a module
constant lowered, for a scope and then put back. A binding whose slot
is named with `attr=` or `item=` is a **value binding**: `overrides()`
makes the slot hold a value, `hides()` makes it absent (which
`overrides(None)` cannot say), and removal restores what `apply()`
found, including restoring absence. A **mapping binding**
(`mode="mapping"`) does the same for a dict's whole content, merging
with `updates()` or replacing with `overrides()`, always mutating the
one real dict in place so every holder of it sees the change. These
bindings hold state rather than wrap operations, so they record
nothing, and say so.

## Iterator bindings: consumption is the event

For a generator or other iterable, the interesting moments are spread
over its consumption, so `mode="iterator"` gives channels for exactly
those: `on_item` as each element passes, `on_finish` when the
iteration completes, `on_error` when it raises, `on_abandon` when the
consumer walks away without finishing. The last one answers a
question nothing else can see asked: the loop that stopped early, the
generator dropped half-consumed. Details are in the
[monkey patching](monkey-patching.md) guide alongside the other
modes.

## Timeline and tape: scoped recording

Bindings intervene; recording is what turns them into evidence. A
**timeline** is a scope: `with wrapture.timeline(...)` opens it, and
while it is open, every call through every applied binding records an
**event**. The record itself is the **tape**, and the two words are
two views of one thing: the timeline is the scope, the tape is what
it holds. Bindings handed to `timeline()` are applied on entry and
removed on exit; bindings applied by other means record onto the open
tape as well.

An event is richer than a mock's call record: signature-normalized
arguments (so `charge(500)` and `charge(amount=500)` assert
identically), the real return value or the exception, the instance
the method was called on, and its position and nesting in the whole
run. On top of that one vocabulary does all the asserting: filters
narrow (`with_args()`, `with_instance()`, `matching()`, `raising()`),
assertions conclude (`assert_once()`, `assert_never()`,
`assert_times()`), `tape.assert_order()` checks sequence across any
bindings, and `tape.tree()` prints the nested call tree.

An event keeps two facts about failure apart. Its `exception` is the
exception that escaped the scope, set by the recording path itself.
Its `caught` sequence holds the exceptions the observed code handled
inside the scope and reported with `wrapture.note_exception()`, the
way a framework's error handler can, the scope itself completing with
a result; `event.failed` is either, and `raising()` matches either. A
noted exception shows in every view as the same `!!` marker an escape
does, after the result, so one line can say both that the scope
returned and that it failed.

Timelines scope as well as record. A tape is never cleared; a test
that wants fresh counts for each phase opens one timeline per phase,
and timelines nest, an inner one reading only what happened inside it
while an outer one keeps the whole run. Scoping, not erasing, is the
recording idiom throughout.

## Expectations: asserting up front

Assertions read the tape after the fact. An **expectation** is the
same claim declared on the binding before the run: `expect_once()`,
`expect_never()`, `expect_times()`, `expect_at_least()`. Expectations
are verified when their scope closes, a timeline exit or a
decorator's teardown, and an expectation with nothing recording is a
loud error rather than a silent pass. They read as a contract at the
top of the test, with the body free of bookkeeping.

## Stand-ins: stub, mock and observed

Sometimes the test must supply the thing being called, because the
code under test receives it rather than importing it. wrapture's
stand-ins follow one taxonomy. A *binding* wraps a callable that
already lives somewhere. **`observed()`** wraps one the test has in
hand, a closure, a callback, a hook about to be registered, leaving
it as the behaviour and recording its calls. **`stub()`** makes up a
callable where the arguments do not matter and the test dictates the
outcome (`returns=`, `raises=`, with `mimics=` borrowing a real
signature and `kind=` making it genuinely a coroutine or generator
function). **`mock(Spec)`** makes up a whole collaborator: an
instance-shaped double of a named class, every method a
signature-checked recording stub, nothing fabricated beyond the spec.

Two threads run through all four. Strictness: signatures are checked,
absent names raise, and nothing is invented on first touch, which is
the deliberate difference from `unittest.mock`'s spec-less `Mock`.
And recording: stubs, mocks and observed callables record to the same
tape as bindings, so one `assert_order()` spans the real and the
supplied alike. The [unit testing](unit-testing.md) guide covers all
of this; [coming from unittest.mock](coming-from-mock.md) maps each
mock idiom to its counterpart here.

## Scoping forms: with, decorators, fixtures

Everything above is scoped somehow, and tests get three equivalent
spellings. The `with` block is the primitive and the most precise
about extent. The **decorators** say the same thing at the function
level: `@wrapture.bound(...)` mirrors a binding's fluent chain and
injects the built binding into the test as a keyword argument;
`@wrapture.taped()` opens a timeline around the call and injects the
tape. Pytest **fixtures** give reuse across tests, and the pytest
plugin supplies an ambient `tape` fixture so test bodies read
identically under any of the three. The forms mix freely; a target
born inside the test body is bound in the body, with the with-block.

## Log capture: messages as events

A log message the observed code emits is an observation like any
other, so `capture_logs()` records standard library logging as
events of kind `"log"`, selected by logger-name pattern and level
threshold. The capture applies like a binding, `timeline()` accepts
it alongside them, and its `events` speak the usual filter-then-
assert vocabulary, with `at_level()` and `with_message()` joining
the family. What no logging fixture can offer comes from the tape's
structure: each message nests inside the call that emitted it, so a
test asserts "this call logged this warning", not "this warning
appeared somewhere", and a trace shows messages in place in the
tree. The same events flow to sinks, so a config file's `[[log]]`
entries put an application's log messages into its trace with no
code changes.

## Block events: phases the code declares

The third event producer, after bindings (calls observed from
outside) and log capture (messages the code emitted), is a
declaration the code makes itself: `wrapture.block(name)` is a
context manager recording the enclosed stretch of code as one event
of kind `"block"`, with the body's duration, an escaping exception,
and keyword arguments plus `annotate()` filling its data. Everything
recorded inside nests under it, so blocks name the phases of an
operation that no callable boundary covers, assertable in tests with
`tape.blocks(name)` and scoped views from `tape.within(event)`, and
rendered as spans by a tracing sink. Like a log statement, the
marker is inert when nothing listens, so it can live in production
code permanently. There is no decorator form: a whole function is a
`"call"` event, via `@observed` or a binding; `block()` marks what
is smaller than a function.

## From tests to tracing

Take the same bindings, remove the test around them, and the
remaining question is where events go. A **sink** answers it:
`Printer` renders calls live, `JSONLines` streams them to disk,
counters aggregate without retaining, and sinks compose with fan-out,
sampling and filtering. Listening can be process-wide or scoped, so a
long-running application can be observed continuously or in slices.

None of that requires the application's cooperation. A
`wrapture.toml` names the targets (down to whole-module discovery)
and the sinks; `python -m wrapture app.py` applies it at launch, and
with [autowrapt](https://github.com/GrahamDumpleton/autowrapt)
installed, `AUTOWRAPT_BOOTSTRAP=wrapture` in the environment does the
same for a plain `python` start, so an application that cannot be
modified or redeployed differently still yields a trace. On top sit
the request-shaped and time-shaped conveniences: [WSGI](wsgi-tracing.md)
and [ASGI](asgi-tracing.md) tracing group events per request, and
[scheduled tracing](scheduled-tracing.md) opens **windows** on a
schedule or trigger, feeding **collectors** that turn a slice of
events into a report. The [ad-hoc tracing](ad-hoc-tracing.md) guide
is the reference for all of it.

## Instrumentation: packaged patching for a target

When the patching a config needs is richer than naming members, it
is an **instrumentation**: a subclass of `wrapture.Instrumentation`
declaring the one target package it covers, the trigger modules
under it, the version range it supports and the settings it takes,
with an `apply(name, module)` that wrapture calls when each trigger
is imported and a `remove()` that undoes it. An `[[instrument]]`
entry names the class, by entry point name for a published package
(one distribution may register many, one class per target) or by
`module:attr` reference for a class next to the config file, and
its other keys are the declared settings, validated at load.
wrapture checks the declaration (triggers under the target, no two
instrumentations for one target, requirements present), gates on the
installed version, reports what each applied instance has done, and
takes it down again on revert; `wrapture.instrumentation(...)` scopes
the same to a block in a test, and `python -m wrapture.tools
instrumentation` lists what an environment has installed and writes
the template to switch it on. [Instrumentation
packages](instrumentation-packages.md) is the author's guide.

## Trace identity: one trace across processes

Event linkage is process local; a **trace identity** extends it. On
by default, every tree of events rooted in an operation, a call, a
request or a block, carries a W3C trace id (W3C trace context is
the one wire format, the one the ecosystem converged on), minted at
the root or joined from an arriving request's `traceparent` header,
shared by every event in the tree and stamped on every serialised
line, so two services both observed by wrapture join their trace
files on one id with no tracing backend involved. Instrumentation
injects the identity into outbound requests through a two-function
public surface, `current_trace()` and `trace_headers()`, and an
identity wrapture parses but nothing claims passes through
verbatim: it never breaks a trace it does not understand. The
`[trace]` config table
switches identities off process-wide, with `trace = true` on an
observe entry as the case-by-case re-enable.

## The map

| Concept | In one line | Where |
|---|---|---|
| Binding | Names one location; carries behaviour and lifecycle | [Monkey patching](monkey-patching.md) |
| Channels and verbs | `on_call`/`on_get`/`on_set`/`on_delete`; stages compose, one terminal decides | [Monkey patching](monkey-patching.md) |
| Phases | Behaviour scripted to change over time with `then()` | [Monkey patching](monkey-patching.md) |
| Value / mapping binding | Holds a value or dict content in place, restored on removal | [Monkey patching](monkey-patching.md) |
| Iterator binding | Wraps consumption: items, finish, error, abandonment | [Monkey patching](monkey-patching.md) |
| Timeline / tape | The recording scope and the record it holds | [Unit testing](unit-testing.md) |
| Events and assertions | Normalized arguments, real results; filter then assert | [Unit testing](unit-testing.md) |
| Expectations | The same claims declared up front, verified at scope close | [Unit testing](unit-testing.md) |
| `observed()` | Wraps a callable the test has in hand; it stays the behaviour | [Unit testing](unit-testing.md) |
| `stub()` | A made-up callable; the test dictates its outcome | [Unit testing](unit-testing.md) |
| `mock(Spec)` | A made-up collaborator, strictly shaped by its spec | [Unit testing](unit-testing.md) |
| `bound()` / `taped()` | The with-block's meaning as decorators, handles injected | [Unit testing](unit-testing.md) |
| Log capture | Log messages as events, nested in the call that emitted them | [Unit testing](unit-testing.md), [Ad-hoc tracing](ad-hoc-tracing.md) |
| Block events | `block()` declares a stretch of code as an event, children nested under it | [Unit testing](unit-testing.md), [Ad-hoc tracing](ad-hoc-tracing.md) |
| Sinks | Where events go outside a test: print, stream, count, compose | [Ad-hoc tracing](ad-hoc-tracing.md) |
| Config and injection | `wrapture.toml` plus a launcher or autowrapt: tracing without code changes | [Ad-hoc tracing](ad-hoc-tracing.md) |
| Instrumentation | A class declaring one target and patching it on import, named from the config | [Ad-hoc tracing](ad-hoc-tracing.md), [Instrumentation packages](instrumentation-packages.md) |
| Request tracing | Events grouped per WSGI/ASGI request | [WSGI](wsgi-tracing.md), [ASGI](asgi-tracing.md) |
| Trace identity | Every operation-rooted tree carries a distributed trace id, propagated over HTTP | [Ad-hoc tracing](ad-hoc-tracing.md) |
| Windows and collectors | Scheduled slices of observation turned into reports | [Scheduled tracing](scheduled-tracing.md) |

If you are new, read [getting started](getting-started.md) next; it
walks the first binding, the first recording and the first pytest
test with everything pasteable into an interpreter. Then follow the
guides in the order above: the mechanism, the testing workflow, the
tracing machinery.
