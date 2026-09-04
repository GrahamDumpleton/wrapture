# Using wrapture in tests

Bindings are ordinary objects with an explicit lifecycle, so they fit any
test framework's scoping tools without special integration. What matters
in a test suite is that every applied binding is removed again, whatever
the outcome of the test: a patch that leaks changes the behaviour of every
test that runs after it.

This page covers the workflow in the order a test suite grows into it:
the scoping patterns for plain tests, pytest and unittest; observing
callables no binding can reach, and the stand-ins a test supplies
itself (`stub()` for one callable, spec-required `mock()` for a
collaborator); recording what
actually happened inside a test as events on a timeline, including how
much of each value is captured, annotation, and stack capture; filtering
and asserting on those events, immediately or as declared expectations;
the whole-tape views of the call tree and ordering; and the optional
pytest plugin that sweeps for leaked patches and attaches tapes to
failure reports.

## Scoping with a context manager

The simplest pattern, and the right default for a patch needed in exactly
one test:

```python
def test_charge_is_stubbed():
    with wrapture.binding(Gateway, "charge").on_call.returns({"id": "stub"}):
        assert place_order("widget")["charge"] == "stub"
```

The patch is applied on entry and removed on exit, including when the test
body raises. Groups scope the same way:

```python
def test_gateway_offline():
    group = wrapture.bindings(charge=wrapture.binding(Gateway, "charge"),
                              refund=wrapture.binding(Gateway, "refund"))
    group.charge.on_call.raises(TimeoutError("down"))
    group.refund.on_call.raises(TimeoutError("down"))

    with group:
        ...
```

## Scoping with decorators

For the test that binds one or two statically addressable targets
around its whole body, `bound()` says the same thing as the with-block
without the nesting level. It takes the same addressing arguments as
`binding()` and mirrors its fluent chain; the result is the decorator,
and the binding it builds is injected into the test as a keyword
argument, the way a fixture would arrive:

```python
@wrapture.taped()
@wrapture.bound(Gateway, "charge").on_call.returns({"id": "stub"})
def test_charge_is_stubbed(tape, charge):
    assert place_order("widget")["charge"] == "stub"

    charge.events.assert_once()
    tape.assert_order(charge)
```

`taped()` opens a `timeline()` around the call and injects the tape;
its default name matches the pytest plugin's `tape` fixture, so under
the plugin `taped()` is unnecessary and the body reads identically.
Each call of the test constructs a **fresh binding**: string targets
resolve late the way the import paths given to `unittest.mock`'s
`@patch` do, every parametrize case gets a clean history, and removal
is owned by the decorator, so it cannot be forgotten.

The injected name defaults to the slot's name, the final segment of
the addressing path: `bound(Gateway, "charge")` injects `charge`.
`alias=` overrides it, and is required, loudly, when the slot's name
is not a valid identifier or when two decorators would inject the same
name. Read a stack of these decorators as a top-to-bottom sequence of
statements about the test below, not as nested wrappers: stages
accumulate in reading order and the last terminal reading down wins,
exactly as the same statements behave on a live binding. Decorators
addressing the same slot collapse into one binding and one injected
handle:

```python
@wrapture.bound(Model, "status").on_get.returns(5)
@wrapture.bound(Model, "status").on_set.raises(AttributeError("read-only"))
def test_reads_pinned_writes_rejected(status):
    ...
```

Every injected name must have somewhere to land: a decorator whose
name matches no parameter is a loud `TypeError` at decoration, never a
surprise at call time. A `**kwargs` parameter absorbs any injected
handles the body does not name, which keeps a decorator-heavy test's
signature down to what it actually reads:

```python
@wrapture.taped()
@wrapture.bound(Gateway, "charge").on_call.returns({"id": "stub"})
@wrapture.bound(Ledger, "record").on_call.returns(None)
def test_asserts_on_the_tape_alone(tape, **_):
    ...
```

Value and mapping bindings work the same way, which makes the
decorator the direct counterpart of `unittest.mock`'s `@patch.dict`.
The slot's name is the injected name exactly, capitalization included,
so an environment variable arrives under its own spelling;
`alias="api_key"` restyles it when the parameter should read as
ordinary Python:

```python
@wrapture.bound(os.environ, item="API_KEY").overrides("sk_test")
def test_priced_call_succeeds(API_KEY):
    ...
```

One caution carries over from ordinary Python. The decorator's
arguments are evaluated once, when the test function is defined,
exactly as default argument values are, and with the same consequence
for mutable values: `overrides([])` in a chain is one shared list. The
binding is fresh on every call, but the value is not, so in the one
situation where a decorated test runs more than once in a process,
above all a parametrized test, whatever one run leaves in that list the
next run inherits. A mutable override that must start clean on every
run is built in the body instead, with the with-block, so the value is
created in the scope of the call it serves:

```python
@pytest.mark.parametrize("payload", PAYLOADS)
def test_each_payload_starts_clean(payload):
    with wrapture.binding("config", attr="WARNINGS").overrides([]):
        process(payload)
        ...
```

Declared expectations ride the chain too, on the spec itself since an
expectation belongs to the binding rather than to a channel, and are
verified when the decorator removes the binding after a passing body,
so verification cannot be forgotten. An expectation with nothing
recording is a loud error rather than a silent pass:

```python
@wrapture.taped()
@wrapture.bound(Gateway, "charge").on_call.raises(TimeoutError("down"))
@wrapture.bound(Ledger, "record").expect_never()
def test_failed_charge_never_reaches_the_ledger(tape, charge, record):
    with pytest.raises(TimeoutError):
        place_order("widget")
```

The chain carries one phase's worth of behaviour: stages plus at most
one terminal per channel. `then()` and `advance()` are deliberately
not part of it; how behaviour changes over time is the test's script,
and it is configured in the body through the injected handle, where
the phase markers can be named:

```python
@wrapture.taped()
@wrapture.bound(Gateway, "charge")
def test_recovery_after_transient_timeout(tape, charge):
    flaky = charge.on_call.then(after=2)
    flaky.raises(TimeoutError("gateway busy"))
    flaky.then(after=1).returns({"id": "fallback"})

    ...
```

Async tests work unchanged, and the binding spans the await, not just
the call that creates the coroutine; `unittest.TestCase` and
`IsolatedAsyncioTestCase` methods are supported too. The decorators
refuse a generator function or a pytest fixture at decoration time:
a fixture's binding would be removed before the tests that use it
run, and the with-block around the fixture's `yield` is the form for
those.

Two boundaries keep the choice of form honest. A decorator can only
address targets that exist at decoration time or resolve by name at
call time: modules by string, classes, module functions. A target born
inside the test body is bound in the body, with a with-block. And the
decorator fixes the recording extent to the whole function body; a
test that wants "nothing before this line counts" keeps the
with-block. The two mix freely, with body bindings recording onto the
decorator's tape alongside the rest.

## Scoping with pytest fixtures

A yield fixture gives the same guarantee with reuse across tests:

```python
import pytest
import wrapture

@pytest.fixture
def stub_charge():
    with wrapture.binding(Gateway, "charge").on_call.returns({"id": "stub"}) as stub:
        yield stub

def test_order_uses_stub(stub_charge):
    assert place_order("widget")["charge"] == "stub"
```

Because the fixture yields the binding, a test can reconfigure it in
flight:

```python
def test_gateway_recovers(stub_charge):
    stub_charge.on_call.raises(TimeoutError("down"))
    with pytest.raises(TimeoutError):
        place_order("widget")

    stub_charge.on_call.returns({"id": "retry"})
    assert place_order("widget")["charge"] == "retry"
```

The same shape suits a value binding, one that holds an environment
variable, a settings entry or a module constant rather than wrapping a
call: apply it in the fixture holding nothing, and let each test say
what the slot should be:

```python
@pytest.fixture
def api_key():
    with wrapture.binding(os.environ, item="API_KEY") as key:
        yield key

def test_missing_key_is_reported(api_key):
    api_key.hides()
    with pytest.raises(ConfigError, match="API_KEY"):
        make_client()
```

A whole settings dict works the same way with `mode="mapping"`: the
fixture applies `wrapture.binding("myapp.config", "SETTINGS",
mode="mapping")` and each test calls `updates({...})` or
`overrides({...})` on it, the dict being changed in place so every
holder of it sees the test's content and the original entries come
back afterwards.

Keep fixtures that apply bindings **function scoped** (the pytest
default). A session or module scoped fixture leaves the patch applied
across every test in between, which reintroduces exactly the leakage the
fixture was meant to prevent; widen the scope only when that is the
intent.

## Scoping with unittest

Use `addCleanup()` rather than `tearDown()`, so removal is registered the
moment the patch is applied and runs even if a later line of `setUp()`
fails:

```python
import unittest
import wrapture

class OrderTests(unittest.TestCase):
    def setUp(self):
        self.charge = (
            wrapture.binding(Gateway, "charge")
            .on_call.returns({"id": "stub"})
            .apply()
        )
        self.addCleanup(self.charge.remove)

    def test_order_uses_stub(self):
        self.assertEqual(place_order("widget")["charge"], "stub")
```

`remove()` is idempotent, so a cleanup that runs after a test already
removed the binding is harmless.

## Sharing binding declarations

Creating a binding does not patch, so declarations can live at module or
class scope and be shared, with each test applying and removing as needed:

```python
charge = wrapture.binding(Gateway, "charge")

def test_one():
    with charge:
        ...

def test_two():
    with charge:
        ...
```

Two things to know when sharing a binding this way:

- **Behaviour persists across apply/remove cycles.** `remove()` restores
  the target but keeps the configured behaviour, so a stub configured in
  one test is still configured when the next test applies the same
  binding. Either configure behaviour in every test that needs it, or
  clear it with `reset()`.
- **One application at a time.** A shared binding cannot be applied twice
  concurrently; the second `apply()` raises `AlreadyAppliedError`. This
  also means a shared binding is unsafe under parallel test runners such
  as pytest-xdist when two workers patch the same target in one process.

## Do not mix scoping styles

Pick one owner for each binding's lifecycle. Entering a binding as a
context manager when something else already applied it raises
`AlreadyAppliedError` rather than silently letting the inner scope remove
the outer scope's patch. If a fixture owns the binding, tests should only
reconfigure behaviour, never call `apply()`, `remove()` or enter it as a
context manager themselves. A `bound()` decorator owns the lifecycle of
the fresh binding it constructs for each call, so its handle is safe to
reconfigure in the body and never needs applying or removing there.

## Discovering members by pattern

`binding()` and `bindings()` name every member explicitly. When the point
is to observe a whole family of members rather than stub one,
`discover()` selects them by pattern and returns a `BindingGroup` keyed
by member name:

```python
def test_gateway_calls_are_recorded():
    group = wrapture.discover(Gateway, "*", exclude="_*")

    with wrapture.timeline(group):
        place_order("widget")

        group.charge.events.assert_once()
```

The target is a module, a class, or a string naming one (`"module"` or
`"module:path"`, the spelling `binding()` and a config file observe
entry use). `match`
is one `fnmatchcase` pattern or a sequence of them, and `exclude`
subtracts from whatever matched. The remaining keyword options
(`capture=`, `capture_args=`, `capture_result=`, `stack=`, `when=`,
`tree=`, `leaf=`, `category=`) are the uniform subset of
`binding()`'s options, applied to every selected member.

Selection is deliberately confined, and is shared with the config file's
`match` entries so a pattern selects the same members however it is
spelt: only the target's own immediate members, never inherited ones,
with no traversal into nested classes or submodules, and only routines
the target itself defines. Properties, other descriptors, plain data,
nested classes, anything already wrapped, and a module's imported
functions are all skipped. `binding()` is the escape hatch that binds
any of the skipped kinds by exact name.

Two behaviours differ from a config observe entry, both because
`discover()` answers "what is here right now" rather than riding a
future import:

- **Discovery is immediate.** Enumerating members requires the target to
  exist, so a string target is imported when `discover()` is called;
  there is no deferral. In a config file the same pattern waits for the
  application to import the module (see
  [ad-hoc tracing](ad-hoc-tracing.md)).
- **An empty selection raises.** A pattern that selects nothing raises
  `ValueError` rather than returning an empty group, so a mistyped
  pattern cannot produce a test that vacuously observes nothing. The
  config file form warns instead, because at startup there is no return
  value to inspect and the process should still come up.

## Observing a bare callable

A binding names a location that can be resolved, patched and
restored: an attribute, or a mapping entry named with `item=` (a
handler in a dispatch table is
`binding(HANDLERS, item="GET", mode="callable")`, removal included).
Plenty of callables never sit at a nameable location: closures,
partials, work put on a queue, callbacks handed over and kept
somewhere private, a view function on its way into a framework's
registration call. `observed()` wraps the value instead of the
location:

```python
wrapped = wrapture.observed(fn)
registry.register(wrapped)
```

The returned proxy records a `"call"` event per invocation inside a
recording scope, nesting in the tree exactly as a bound call does,
and calls straight through when nothing listens. Its path derives
from the callable itself (`module:qualname`); no label is derived,
so events carry label None and every renderer falls back to the
path, with `label=` to assign a name where one adds something the
path cannot say. The keyword options are the uniform subset
`binding()` takes: `capture=`, `capture_args=`, `capture_result=`, `stack=`,
`when=` (which receives `(instance, args, kwargs)`; the instance is
None for a free-standing callable), `tree=`, `leaf=`, `category=` and
`data=`, the last three also as per-call resolvers with `when=`'s
signature, as on a binding.

Placed on a class, the proxy binds
as a method exactly as the wrapped function would: calls made through
instances record the instance (so `with_instance()` applies), the
bound signature drops `self`, and the normalized arguments never
contain it.

It is equally the decorator for functions you own, in both the usual
spellings: bare `@wrapture.observed` directly on a def, and
`@wrapture.observed(label="charge", capture="summary")` when options
are wanted, where the call with only keyword arguments returns the
decorator that captures the function below it. Everything but the
callable itself is keyword-only.

The division of responsibility is the inverse of a binding's. A
binding owns installation and removal; `observed()` owns neither:
there is no `apply()` or `remove()`, you place the proxy wherever the
original was going, and putting the original back is equally your
job. Everything else about a binding's character is kept: `suspend()`
and `resume()`, the honest counters (`suspended_calls`,
`filtered_calls`, `missed_calls`), and `events` for assertions:

```python
def test_each_registered_hook_ran_once():
    hooks = [wrapture.observed(fn) for fn in registry.callbacks]
    registry.callbacks[:] = hooks

    with wrapture.timeline():
        registry.fire()

        for hook in hooks:
            hook.events.assert_once()
```

The proxy is deliberately transparent: `__name__`, `__doc__`,
signature introspection, coroutine-function detection and equality
all delegate to the wrapped callable, so registries that inspect what
they are handed behave as if the wrapper were not there. That makes
`observed()` safe to interpose at a framework's registration choke
point; the flask-app example in the repository's
[examples directory](https://github.com/GrahamDumpleton/wrapture/tree/main/examples)
wraps every view function as `Flask.add_url_rule` registers it, via
one `transforms_args` stage.

### Applying dynamically without double-wrapping

Wrapping the same thing twice is the classic monkey-patching
accident, so `observed()` builds reliable detection in: the
observation's identity is its assigned label, or its derived path
when unnamed. Before wrapping, the callable's full wrapper chain is
inspected with wrapt's `wrapper_chain()`, which sees through proxies
and `functools.wraps()` decorators alike; if an `ObservedCallable`
layer already carries the same identity, that observation is already
applied, however deeply a later wrapper buried it, and the callable
comes back unchanged. Distinct identities stack, each layer
recording its own event, one nested under the other, and stacking by
accident cannot be told apart from two agents observing on purpose,
so it is not an error: it shows up honestly as double counting in
the results.

With no label given, the derived path serves as the identity, which
is enough for the simple wrap-in-place idiom to re-run safely:

```python
registry[key] = wrapture.observed(registry[key])
```

But the derived path is read from the object handed in, before the
chain is walked, and that is a trap when other wrappers intervene: a
third-party wrapper that exposes `__wrapped__` without preserving
introspection (as `functools.wraps` would) changes what the path
derives to, so it no longer matches the buried layer's identity and
the dedupe silently misses. Wherever double wrapping is a real risk,
always use a pre-determined label you specify, a distinctive prefix
works well; it is a constant compared against stored identities, so
it survives introspection loss as long as the chain is walkable at
all (avoid a colon in a label: in every output a colon marks a real
`module:qualname` path, and a label is precisely not that):

```python
registry[key] = wrapture.observed(registry[key], label=f"myagent.{key}")
```

A wrapper that hides `__wrapped__` entirely blinds the walk, and no
label can see past it; the mitigation there is structural, observing
at a choke point where you wrap before anything else does. To detect
without wrapping, walk the same chain the dedupe walks:

```python
def has_observer(fn, label):
    return any(
        isinstance(layer, wrapture.ObservedCallable) and layer.label == label
        for layer in wrapt.wrapper_chain(fn)
    )
```

One boundary. `observed()` is observation only: there are no
behaviour namespaces, so it cannot stub or fail-inject; an
intervention wants a removable home, and a free-floating callable has
none, so use `binding()` for those. Writing `@observed` inside the
application, by contrast, is a posture wrapture supports on purpose:
authors who prefer their instrumentation next to the code it marks
decorate their own functions, with the config route as the
counterpart for code they do not own. The
[embedded instrumentation story](ad-hoc-tracing.md#instrumenting-your-own-code)
gathers that surface in one place.

## Supplying a stand-in with stub()

Sometimes there is no callable to wrap: the test itself must supply
one. A hook slot read by name, a receiver handed to a signal's
`connect()`, a callback passed into a registration call. Usually the
test does not care what arguments arrive there; it wants to count the
calls, or dictate the outcome, and nothing else. `stub()` builds that
stand-in and returns it already observed, with the same recording
character as everything else: `events`, `suspend()`/`resume()`, the
honest counters, timeline participation.

```python
hook = wrapture.stub("before_start")

service = Service(on_ready=hook)

with wrapture.timeline():
    service.start()
    hook.events.assert_once()
```

A bare stub accepts any arguments and returns None; `returns=` makes
every call produce a value, `raises=` makes every call raise, and the
two are mutually exclusive. There are no phases and no `on_call`
namespace: a stand-in whose behaviour must change over time has
outgrown a stub and should be a real function, or a binding on a real
location. And a stub stands in for one callable only: it fabricates no
attributes and never widens into an object.

The permissiveness is the point, and it is deliberately the inverse of
the package's default. A binding on a real callable is strict first:
calls are checked against the signature and recorded by name. Reaching
for a bare `stub()` is the explicit statement that arguments do not
matter here. `mimics=` is the opt back in:

```python
hook = wrapture.stub(mimics=Task.before_start)
```

borrows two things from the callable it names. The signature: calls
are checked against it, a call that does not fit raises `TypeError`
before anything is recorded (exactly as a strict binding rejects a
drifted call), and events record arguments by parameter name, so
`with_args(task_id=...)` matches them. And the kind: a stub mimicking
an `async def` is itself a coroutine function, detected as one through
the proxy and awaited like one.

For a stand-in with nothing to mimic, `kind=` states the calling
convention directly: `"function"` (the default), `"generator"`,
`"coroutine"` or `"async_generator"`. The stand-in genuinely is a
callable of that kind, and the outcome arrives as that kind delivers
it:

```python
fetch = wrapture.stub("fetch", kind="coroutine", returns=payload)   # resolves on await
stream = wrapture.stub("stream", kind="async_generator", returns=[1, 2])   # yields on async for
items = wrapture.stub("items", kind="generator", returns=[1, 2])    # yields on for
down = wrapture.stub("down", kind="coroutine", raises=TimeoutError("down"))   # raises on await
```

For the generator kinds `returns=` is the iterable of items to yield,
and `raises=` fails the iteration rather than the call. Events keep
the call/completion split, so `events.finished()` is the awaited (or
fully consumed) subset and `events.pending()` catches the coroutine
that was created and never awaited; where `unittest.mock` needs the
separate `AsyncMock` class for all of this, the stub's kind is one
argument, inferred whenever `mimics=` names the real thing.

Placed on a class, a stub binds as a method like any observed
callable: calls through instances record the instance, and a mimicked
signature's `self` is accounted for by the binding, exactly as with
the real method. The
[supplying hooks and collaborators](example-supplied-stand-ins.md)
example builds a full test around stubs and doubles.

## A collaborator double with mock()

A stub stands in for one callable. When the code under test takes a
whole object through a seam, a constructor argument, a function
parameter, an attribute, and calls several methods on it, the stand-in
is a collaborator, and `mock()` builds one. The fence comes first: **a
mock requires a spec and fabricates nothing beyond it.** There is no
spec-less form.

```python
class Connection:
    def channel(self) -> "Channel": ...
    def close(self) -> None: ...

class Channel:
    def basic_publish(self, body, routing_key="task"): ...

channel = wrapture.mock(Channel)
conn = wrapture.mock(Connection)
conn.channel.returns(channel)          # the object graph, declared

service = Service(conn)                # injected through the seam

with wrapture.timeline() as tape:
    service.send("hi")

    channel.basic_publish.events.with_args(body="hi").assert_once()
    tape.assert_order(conn.channel, channel.basic_publish, conn.close)
```

Every method of the spec, inherited ones included, is a stub that
mimics the real method: calls are checked against its signature and
raise `TypeError` on drift, events record arguments by parameter name
under `Connection.channel`-style labels, and the method's kind carries
over from the spec per method, so an `async def` method is awaited (and
`events.finished()`/`events.pending()` distinguish awaited from
called-and-forgotten), a generator method is iterated, and a class
mixing all of them just works: the spec says which is which, so it can
never be got wrong.

What a mock deliberately does not do is fabricate. Every method returns
None until `returns()` or `raises()` configures it; there are no
auto-created children and no return-value chains, so the object graph a
test depends on is declared, one double per node. Accessing a name the
spec does not have raises `AttributeError`, in the test and in the code
under test alike, and data attributes hold no invented values: the test
assigns what the code reads (`conn.host = "amqp.local"`), and reading
an unassigned one is a loud error. A bare assignment lasts for the
double's lifetime; when the value should hold for only part of the
test, a value binding on the double scopes it,
`with wrapture.binding(conn, attr="host").overrides("amqp.local"):`,
and on exit the attribute is absent again, loud reads included.
Compare the classic `unittest.mock` failure mode:

```python
# unittest.mock: everything below passes, no spec was asked for
conn = Mock()
service = Service(conn)
service.send("hi")
conn.chanel().basic_publish.assert_called()   # typo'd chanel: passes

# wrapture.mock(Connection): the same typo is an AttributeError at the
# call site inside Service, and an unconfigured channel() returns None,
# so the chained call fails loudly instead of inventing a channel
```

(Spec'd mock, `Mock(spec=...)` or `create_autospec`, catches much of
this too, per-method async detection included; the difference is that
in wrapture the spec'd path is the only path.)

The double passes `isinstance(conn, Connection)`, since it stands in
for exactly that class (`type(conn)` still tells the truth), and when
the spec is a context manager, `with conn:` enters to the double and
exits inertly. A double is a value like a stub: the test places it and
owns its lifetime. To substitute a class at a location, so code that
constructs its own collaborator gets doubles, hold a factory in a
value binding:

```python
with wrapture.binding("myapp.transport", attr="Connection").overrides(
    lambda *a, **k: wrapture.mock(Connection)
):
    ...
```

## Recording calls on a timeline

A binding does more than intervene: inside a recording scope it also
observes. `timeline()` opens that scope, applies the bindings it is given,
and yields a tape; on exit the bindings are removed again. The recording
scope and the useful patch lifetime are the same interval, which is why
the two are one construct:

```python
def test_order_writes_the_ledger():
    charge = wrapture.binding(Gateway, "charge")
    record = wrapture.binding(Ledger, "record")

    with wrapture.timeline(charge, record) as tape:
        place_order("widget")

    outer, inner = tape.all
    assert outer.path == "myapp.gateway:Gateway.charge"
    assert inner.path == "myapp.ledger:Ledger.record"
    assert inner.parent_id == outer.seq
```

`timeline()` accepts bindings, binding groups, or iterables of either, and
with no arguments it only records, for bindings whose lifetime is managed
elsewhere. An applied binding with no timeline open (and no other sink
listening, see [ad-hoc tracing](ad-hoc-tracing.md)) records nothing and
costs almost
nothing beyond wrapt's own dispatch, so leaving bindings applied while
only occasionally recording is a supported pattern, not a mistake.

### Recording only the part you care about

A tape is never cleared: it is the record of what happened while the
timeline was open, and an assertion reads it as it stands. When a test
has setup that itself goes through the bound code (a constructor that
pings the gateway, a fixture that seeds data) and the assertions are
about the act step alone, the answer is not to erase the setup's
events (the idiom `reset_mock()` serves in `unittest.mock`) but to open
the timeline around the act step only. The simplest form is the
fixture shape above: the fixture applies the binding, and the test body
opens `timeline()` around the call under test. Where the setup must
happen inside the recording scope, timelines nest, and an inner
`timeline()` with no arguments records only what happens inside it,
with `binding.events` reading the innermost tape:

```python
with wrapture.timeline(charge, record) as whole:
    service = make_service()            # its calls land on `whole` only

    with wrapture.timeline() as act:
        service.place("widget")
        charge.events.with_args(amount=500).assert_once()   # the act step alone
```

The same scoping is how a **phased test** keeps each phase's counts
separate, the other job `reset_mock()` does in a mock suite: act,
assert, change something, act again. Open one timeline per phase. A
binding handed to `timeline()` is applied on entry and removed on
exit, so the same binding serves every phase, and each phase's tape
starts empty without any history being destroyed:

```python
charge = wrapture.binding(Gateway, "charge").on_call.returns({"id": "stub"})

with wrapture.timeline(charge):
    service.place("widget")
    charge.events.assert_once()      # the first order pays

with wrapture.timeline(charge):
    service.place("widget")          # already paid: served from the cache
    charge.events.assert_never()
```

The second phase states `assert_never()` outright, where one
cumulative tape could only say "the count is still one". Under an
ambient tape (the plugin's, or `taped()`) the same blocks nest as
above, and the outer tape still holds the whole run, so per-phase
counts and whole-test ordering assertions coexist.

A loop of scenarios opens a fresh `timeline()` per iteration the same
way. Behaviour is separate from recording and is reconfigured in place,
`on_call.returns(...)` again, `passes_through()` or `reset()`, without
touching any tape.

### What one event contains

Every call through a binding inside the scope records one event. The
fields a test typically reads:

- `path` is the fully qualified location of what was bound, in
  `module:path` form with both halves dotted (the convention setuptools
  entry points use), so two same-named classes in different modules stay
  distinguishable and the event remains self-describing if it ever
  leaves the process. `label` is the `label=` given to the binding, or
  None when none was: no name is ever derived, so wherever a combined
  name appears in output, a colon means the real `module:qualname`
  location and its absence means a name somebody assigned. Assert
  against `path` routinely, `label` when one was assigned.
- `instance` is the object the method was called on.
- `args` and `kwargs` are the call as the caller wrote it, and
  `arguments` is the signature-normalized form with defaults applied, so
  `charge(500)` and `charge(amount=500)` record identically. Assert
  against `arguments`.
- `result` holds the return value, or the `MISSING` sentinel
  (`wrapture.MISSING`, re-exported from wrapt) when the call raised
  instead; `exception` holds the exception that propagated. A call that
  returned None records `result=None`, which stays distinguishable from
  no result at all.
- `parent_id` and `depth` place the event in the call tree, and `seq` is
  its position in overall recording order. The parent link is the
  enclosing event's `seq`, an integer rather than a reference, so an
  event stands alone if it ever leaves the process; `tape.parent_of()`
  and `tape.children_of()` resolve the links back to event objects when
  a test wants to walk the tree.
- `started` and `duration` place the event in time: when the operation
  began (on the `perf_counter` clock) and how long it ran, exceptions
  included. Recording's own bookkeeping is excluded from the figure.
  Generators refine this with a second number, covered below.
- `thread_id` and `thread_name` record the thread the operation began
  on; a generator or coroutine may run and complete elsewhere.

Events record what actually flowed, behaviour included. A call stubbed
with `returns()` records the stubbed result; a failure injected with
`raises()` records that exception. When `transforms_args()` rewrote the
arguments, the event keeps both sides: `args` as the caller sent them,
and `forwarded` as the `(args, kwargs)` the wrapped function actually
received. No substitution-based mock can record that distinction, because
replacing a function discards what it would have been called with.

Calls on `async def` targets record the awaited outcome, not the
coroutine object: the event completes when the coroutine does, and calls
made inside its body nest under its event even when other tasks run in
between. Concurrent asyncio tasks each record their own correctly nested
subtree onto the shared tape.

An event therefore has two moments, the call and the completion, and
`Event.finished` says whether the second has happened: the call
returned or raised, the generator closed, the coroutine was awaited.
`events.finished()` and `events.pending()` filter on it, and
`tape.pending` counts the open events on the tape (shown in the repr
as `<Tape: 3 events, 1 pending>` while non-zero). For an `async def`
target that is how to catch the classic mistake of calling without
awaiting, which otherwise surfaces only as Python's `RuntimeWarning`
when the coroutine is collected:

```python
with wrapture.timeline(send) as tape:
    await service.notify("hello")

    send.events.assert_once()                 # called once
    send.events.finished().assert_once()      # and awaited
    send.events.pending().assert_never()      # nothing created and forgotten

assert tape.pending == 0
```

The count is live: a coroutine awaited after the scope exits leaves
it. A generator still being consumed, or a call in flight on another
task, is pending for the same reason, so the filters read as "has this
operation ended" rather than anything specific to coroutines.

The tape closes when its timeline exits. Work that outlives the scope,
a task never awaited or a thread still running, is discarded from then
on and counted on `Tape.discarded`, so the tape a test asserted on
cannot quietly grow afterwards; a non-zero count shows in the tape's
repr as `<Tape: 3 events, 1 discarded after close>` and is the clue
that the test finished before its work did. Entering the same timeline
again reopens the tape.

### Attribute events

Attribute bindings record onto the same tape, as events of kind `get`,
`set` and `delete`, read through the same `events` property and narrowed
with `of_kind()` when a test cares about one kind.

Suppose the code under test stores its outcome in an attribute:

```python
def publish(model):
    ...
    model.status = "published"
```

A binding on the attribute observes that write happen, so the test can
assert on it without knowing anything about how `publish()` works
inside:

```python
def test_publishing_writes_the_status_once():
    status = wrapture.binding(Model, "status", missing_ok=True)

    with wrapture.timeline(status):
        publish(model)

        status.events.of_kind("set").with_value("published").assert_once()
```

What each kind records:

- A `get` event records the value read in `result`, the same field a
  call's return value uses. That is why `returning()` works on both:
  "what came out" is one question, whether it came out of a call or a
  read.
- A `set` event records the value the caller wrote in `value`. If
  `on_set.transforms()` rewrote the value on the way through, `value`
  still holds the caller's original.
- A `set` or `delete` event also records the old value in `previous`,
  but only when that was free to know, meaning the old value sat in the
  instance dictionary. When the prior definition is a property, reading
  the old value would mean running its getter behind your back, so
  `previous` stays unrecorded.

Capture policies apply here too. A written value is captured under the
attribute's name, so `redact("password")` masks writes to an attribute
called `password` the same way it masks a parameter called `password`.

Attribute and call events nest together on the tape. If a property's
getter calls an observed method, the `get` event is the parent and the
call event is its child, so `tree()` shows which read triggered the
work. That is exactly the question a lazy-loading bug usually turns on.

One reminder from the known limitations page: only access through an
instance records. Reading the attribute off the class returns the
descriptor without firing it, so nothing is recorded.

### Generators and iteration

Calling a generator function does not run its body; it returns a
generator that runs later, a little at a time, as the consumer iterates.
Recording follows what actually happens: the call records **one event
covering the whole iteration**, not an event per item. A binding on a
method that yields thousands of rows still records once, and
`assert_once()` still means "called once".

The event opens when the call creates the generator and fills in as the
iteration proceeds:

- `items` counts the values yielded so far. It is live: a test can read
  it mid-iteration.
- At exhaustion, `result` records the generator's return value, which is
  `None` for the common generator that just yields. Async generators
  cannot return a value, so exhaustion records `None` there too.
- `duration` is wall time from the call to the close of the iteration.
  For a lazily consumed generator that includes all the time the
  *consumer* spent between items, which could be an entire request, so
  there is a second figure: `body_duration` is the accumulated time the
  generator body itself ran, summed over resumptions. Wall time answers
  "how long was this iteration alive"; body time answers "how much work
  did it do".

Nesting also follows what actually happens. The generator body only
runs while the consumer asks for the next item, so observed calls made
*inside the body* nest under the generator's event, while the
consumer's own work between items does not, even though it happens
between the generator's first and last breath. The `tree()` for a loop
over `stream()` that calls `handle()` on each item shows `fetch()`
(called by the body) under the stream and `handle()` (called by the
consumer) beside it.

If the consumer stops early, by `break`, by `close()`, or just by
dropping the generator, exhaustion never happens. The event closes with
its durations and item count but **no result**: on the tape it is
visibly an iteration that never finished, rather than one that
completed quietly. An abandoned generator is frequently a bug, so the
unfinished look is the honest signal, and `result is MISSING` after
close distinguishes it from a finished iteration's `None`.

Item *values* are deliberately not captured, at any capture level: a
long stream would retain every item on the tape, and no policy can
guess which items matter. When a test needs item data, it says so
itself: `annotate()` from a `decorates()` handler, or an `iterator()`
proxy's `on_item` stages, both let the code that knows what matters
keep an immutable copy of exactly that. The `iterator()` proxy itself
records nothing: it has no target and so no identity on a tape. It is
behaviour plumbing, and it composes with recording. Behaviour runs
first, so when a bound call returns a proxied generator the recording
relay wraps the proxy: the consumer drives the recording relay, which
drives the proxy, which drives the real generator. The binding records
the iteration as the consumer experienced it (items counted after the
proxy's transforms, body time including the proxy's stages), while the
return value, exceptions, and close all thread through both levels.

One consequence of recording iteration at all is worth stating plainly:
while a timeline is active, the consumer of a recorded generator
receives the recording relay, not the original generator object. The
full protocol is preserved, but object identity and introspection
details differ, the same way a wrapped function is a `FunctionWrapper`
rather than the original function. Outside a timeline the original
generator is returned untouched.

### What does not record

Four situations produce no event, each deliberate:

- **Outside a timeline** nothing records, but configured behaviour still
  applies: a stub is a stub whether or not anyone is recording.
- **While suspended** a binding records nothing, but counts the calls it
  skipped in `suspended_calls`, so a shorter-than-expected tape can be
  explained rather than silently wrong.
- **Calls triggered by the recording machinery itself** are not recorded,
  which is what keeps an observed callable safe to use anywhere, even
  inside the recorder. Behaviour still applies to such calls: code
  stubbed out stays stubbed out.
- **Calls on a thread without the caller's context**, on builds where
  threads do not inherit it. These raise `RecordingGapWarning` and are
  counted in `missed_calls`, so the gap is loud rather than silent. To
  opt a thread in, wrap its target with `wrapture.propagate()`, or
  with `wrapture.detach()` for work the caller does not wait for,
  which records as its own linked tree; the known limitations page
  covers the details.
- **Operations a `when=` predicate declined.** A binding created with
  `when=fn` consults `fn(instance, args, kwargs)` per operation while
  recording is active, before any event is constructed; a falsey
  answer skips recording that operation and counts it on
  `filtered_calls`. Deliberate silence, but counted. As with wrapt's
  `enabled`, a boolean is accepted in place of the predicate:
  `when=False` makes a behaviour-only binding that never records and
  counts nothing, for plumbing that must not put itself in the trace.
  With `tree=True` a decline also silences everything beneath the
  operation, and the inner bindings count the skip on their own
  `filtered_calls`. The [ad-hoc tracing page](ad-hoc-tracing.md)
  covers both fully.

### How much is captured

By default, values are recorded *by reference*, which is exactly what
`unittest.mock` does: free, and accurate as long as nothing mutates the
value after the call. Code that does mutate its inputs makes a
by-reference record lie retroactively, so capture is a policy, set per
binding:

```python
record = wrapture.binding(Ledger, "record", capture="summary")
```

The levels, named by string and ordered by cost:

- `"none"` records the event but no values: the call stays visible, its
  arguments and result do not, and the signature binding that dominates
  recording cost is skipped entirely.
- `"types"` stores type names only (`<list>`), and never calls user
  code.
- `"reference"` stores references; the default.
- `"summary"` stores a bounded, type-aware repr. It survives locks and
  sockets and retains nothing, but repr is user code and may have side
  effects: summarising a lazy ORM object can issue the very query being
  observed.
- `"snapshot"` deep-copies for full fidelity, falling back to the
  summary form for values that refuse (locks, sockets, connections)
  rather than failing the call under test.

Arguments and results are separate axes, since one is a time cost and
the other a retention cost: `capture=` sets both, and `capture_args=` /
`capture_result=` override it individually.

A policy is either a level named above or any callable
`fn(name, value)` returning what to store, called once per captured
value with the parameter's name (None for a result). Writing one is
ordinary code, and the levels' building blocks, `summarize()` and
`type_name()`, are importable from `wrapture.capture` for use inside
it:

```python
from wrapture.capture import summarize

def masked(name, value):
    if name == "card_number":
        return "<redacted>"
    if name == "metadata":
        return summarize(value, limit=50)
    return value

charge = wrapture.binding(Gateway, "charge", capture_args=masked)
```

The common case, masking named parameters and capturing the rest at
one level, is packaged as `redact()`:

```python
charge = wrapture.binding(
    Gateway, "charge",
    capture_args=wrapture.redact("card_number", level="summary"),
)
```

Redaction matches by parameter name against the normalized arguments,
so positional and keyword calls redact identically, and everything not
named is captured at `level=` (`"reference"` unless given). Results
have no parameter name, so pair it with `capture_result="none"` when
the secret comes back out.

The same vocabulary reaches URL query strings through
`capture_query(query, policy="reference")`, which returns the form the
request middlewares record under `query`: each parameter decoded and
captured under its own name through the policy, with the built-in set
of sensitive names (passwords, tokens, keys, session ids, signatures)
redacted whatever the policy says, and the marker alone if the string
cannot be processed. It is there for code recording the outbound side
of a request, an HTTP client's instrumentation say, so that a `url`
can be recorded without its query and the query beside it with the
same protection the inbound side has.

One bookkeeping note for custom callables: the level recorded on
`event.capture` is read from an optional `.level` attribute on the
callable, defaulting to the reference level; `redact()` sets it for
you. The numeric level constants behind the string names live in
`wrapture.capture` for policies that want them.

A result supplied by `returns()`, `raises()` or `rejects()` is recorded,
not suppressed, because the stubbed value is precisely what flowed
downstream; the event is marked instead. `tree()` renders the mark as
`(injected)`, and `events.injected()` / `events.injected(False)` filter
on it.

### Annotating events with your own data

Where the capture policy is a blanket setting, annotation is targeted:
from inside a `decorates()` handler, or anywhere in observed code,
attach what a generic policy cannot infer:

```python
def around(wrapped, instance, args, kwargs):
    wrapture.annotate(item_count=len(args[0]),
                      items=tuple(args[0]))   # caller's own immutable copy
    return wrapped(*args, **kwargs)
```

`annotate(**data)` merges into the in-flight event's `data` dict, which
filters like anything else:
`events.matching(lambda e: e.data.get("item_count", 0) > 100)`. Outside
recording it is a silent no-op, so observed code can call it
unconditionally. `current_event()` returns the in-flight event itself,
or `None` when nothing is recording.

### Noting a caught exception

An event's `exception` is the exception that escaped the scope. Code
that catches an exception and handles it leaves the scope completing
normally, with a result and no exception, which is the honest record
of control flow but not of the failure; a framework whose error
handler turns a `KeyError` into a 500 response is the common case,
and the handler, passed the exception as an argument, is the only
place it can be seen. `note_exception()` is the sibling of
`annotate()` for that place: it attaches the exception to an event
without changing control flow.

```python
def handling(wrapped, instance, args, kwargs):
    wrapture.current_event(kind="request").note_exception(args[0])
    return wrapped(*args, **kwargs)

handle = wrapture.binding(App, "handle_exception").on_call.decorates(handling)
```

The noted exceptions land on the event's `caught` tuple, one
`CaughtException` per note carrying the exception and the moment it
was noted, distinct from `exception`, so the two facts never blur:
the scope returned, and a failure was noted against it. `event.failed`
answers "did this operation fail, however the failure surfaced", and
`raising()` widens to match: `events.raising(KeyError)` finds the
request whether the framework swallowed the error or let it escape.
`tree()` shows a noted exception as the same `!!` marker after the
result, so one line says both:

```text
GET /quote/missing (app)  -> '500 INTERNAL SERVER ERROR'  !! KeyError
```

The bare `wrapture.note_exception(exc)` notes against the in-flight
event, which from inside a bound handler is the handler's own call;
the unit of work that failed is usually further out, which is what
the two filters on `current_event()` are for.
`current_event(kind="request")` selects the nearest enclosing event
of that kind, the request wrapture's own middleware recorded;
`current_event(binding=dispatch)` the nearest enclosing event that
the given binding recorded, for a unit of work your own binding
created. Either walks outward from the innermost in-flight event,
and given both requires both. The result is always a handle: reads
pass through to the event, `annotate()` and `note_exception()` are
its verbs, and when nothing matched it is empty, falsy, and its
verbs quietly do nothing, so aimed calls need no guard (the bare
`annotate(**data)` and `note_exception(exc)` are shortcuts for the
same verbs on `current_event()` with no filters). Noting the same
exception object twice against one event records it once, and an
exception noted against an event that it then escapes shows once,
as the escape. Outside recording the call is a silent no-op; a note
aimed at an event that has already finished is refused with a
`ConfigWarning`, since the sinks have already heard that event
close.

### Capturing the call stack

The tape's parent and child links give the logical path between
observed points; they say nothing about the unobserved frames in
between. Stack capture gives the physical route, answering "which line
of code triggered this", and is priced per binding:

```python
charge = wrapture.binding(Gateway, "charge", stack="caller")
```

- `stack=None`, the default, captures nothing and costs nothing.
- `stack="caller"` captures just the calling frame, adding a few
  hundred nanoseconds to each recorded event: the sweet spot, since
  "who touched this?" is usually the whole question. Recording an event
  already costs single-digit microseconds, so this is a small fraction
  on top.
- `stack=5` captures that many frames, walking outward from the caller,
  with cost growing roughly per frame.
- `stack="full"` captures everything, for diagnosis rather than
  routine use.

The event stores a small integer, `event.stack`, rather than the frames
themselves. Captured stacks are interned: identical stacks, and stacks
repeat almost perfectly at a given call site, share one entry in a side
table, so per-event storage stays tiny however hot the call. Resolve
the id with `stack_frames()`:

```python
event = charge.events.first
for frame in wrapture.stack_frames(event.stack):
    print(f"{frame.filename}:{frame.lineno} in {frame.function}")
```

Frames are innermost first, and wrapture's and wrapt's own machinery
frames are elided, so the first frame is the code that actually made
the call. Stack capture pairs especially well with attribute events:
`stack="caller"` on a lazy-loading property names the exact
source line that triggered the load, which is normally the hard part
of diagnosing an accidental query.

The side table is bounded. Past ten thousand unique stacks, a level
of churn normal code never reaches, new uniques intern to one shared
overflow marker rather than growing the table, so its memory is
bounded for the life of the process. For long-running processes,
`clear_stacks()` empties the table at a natural flush point (a trace
file rotated away, a tape discarded); ids on events recorded before
the clear stop resolving, with `stack_frames()` raising `KeyError`
for them, so clear only when older events will no longer be
consulted.

## Filtering and asserting on events

A binding's `events` property is the usual way to read the tape: a
filterable view over the enclosing timeline's events for that one
binding. It works inside the `with` block, after the code under test has
run:

```python
def test_failed_charge_is_not_recorded_in_the_ledger():
    charge = wrapture.binding(Gateway, "charge")
    record = wrapture.binding(Ledger, "record")
    charge.on_call.raises(TimeoutError("down"))

    with wrapture.timeline(charge, record):
        with pytest.raises(TimeoutError):
            place_order("widget")

        charge.events.raising(TimeoutError).assert_once()
        record.events.assert_never()
```

Where an empty log would lie, access is a loud error instead: reading
`events` raises `NeverAppliedError` if the binding was never applied,
and `RuntimeError` outside a timeline. "Recorded nothing" and "was not
recording" can therefore never be confused.

### One naming rule for filters

> A method whose name starts with `assert_` raises, immediately. One
> starting with `expect_` declares, and is checked when the timeline
> exits. Everything else returns data.

The rule holds on every object in the package, so a single line read out
of context still says whether and when it can fail a test. A mistyped
assertion name is an `AttributeError`, never a silent pass.

### Filters narrow, and never raise

Each filter returns a new, narrowed `EventLog`, so filters chain freely:

- `of_kind("call", "get", ...)` narrows by event kind.
- `of_category("external", ...)` narrows by the category the event's
  binding declared.
- `matching(predicate)` keeps events the predicate accepts.
- `raising(TimeoutError)` keeps events that raised one of the given
  exception types; `raising()` keeps events that raised anything.
- `with_args(amount=500)` keeps calls whose *normalized* arguments
  include the given values, so `with_args(currency="USD")` also matches
  calls that relied on the default. A name that is not a parameter
  falls through into the target's `**kwargs` bundle when it has one:
  against `def dispatch(job, **options)`, `with_args(priority=5)`
  matches a call that passed `priority` through `**options`, other
  keys free, while `with_args(options={"priority": 5})` names the
  bundle parameter itself and compares the whole mapping.
- `with_instance(obj)` keeps calls made on exactly that object, by
  identity rather than equality, so two equal-but-distinct instances
  seen through one class-level binding stay distinguishable; a
  classmethod's calls record the class, so `with_instance(SomeClass)`
  is "calls made on this class". It compares the live reference the
  event holds, so it is a filter for in-timeline assertions rather
  than for records reloaded with `load_events()`.
- `returning(value)` keeps events whose outcome was the value: a call's
  return value, or the value an attribute read produced.
- `with_value(value)` keeps attribute writes of that value.

A filter that does not apply to an event narrows to empty rather than
raising, so mixed logs stay safe to filter. The risk that creates, a
silently empty log after filtering the wrong thing, is answered in the
failure output: it shows the nearest non-empty log in the filter chain,
so the discarded events are visible:

```
AssertionError: expected exactly 1 event(s), got 0
<EventLog Gateway.charge[amount=999]: 0 event(s)>
    (no events)
  filtered from:
    <EventLog Gateway.charge: 1 event(s)>
        Gateway.charge(amount=500, currency='USD')
```

### Asserting on the recording

The assertions are `assert_never()`, `assert_any()`, `assert_once()`,
`assert_times(n)`, `assert_at_least(n)` and `assert_at_most(n)`. Each
raises `AssertionError` with the events in the message, and returns the
log so a passing assertion can keep chaining:

```python
event = charge.events.with_args(amount=500).assert_once().first
assert event.forwarded is None
```

For those who prefer pytest's bare `assert`, a log is falsey when empty
and sized like a list, and its repr prints the events:

```python
assert charge.events.with_args(amount=500)
assert charge.events.count == 2
```

### Declared expectations

An assertion is written where it runs; an expectation is stated once, up
front, and verified automatically when the timeline exits, so
verification cannot be forgotten:

```python
def test_order_charges_exactly_once():
    charge = wrapture.binding(Gateway, "charge").expect_once()

    with wrapture.timeline(charge):
        place_order("widget")
    # exiting verifies: ExpectationNotMetError if there was not
    # exactly one charge
```

Available: `expect_times(n)`, `expect_once()`, `expect_never()` and
`expect_at_least(n)`. Several can be declared and all are verified; the
timeline verifies the bindings (and group members) it was given, and a
`bound()` decorator verifies its own binding's declarations, chained
or made in the body on the injected handle, when it removes the
binding after a passing body.
`ExpectationNotMetError` derives from `AssertionError`, so test
frameworks report it as a failure, with the same event-listing output
the immediate assertions produce.

Verification is skipped when the block raised: the in-flight failure is
the real cause, and a verification error on top would bury it.

Like behaviour, expectations persist on the binding across apply and
remove cycles: a shared declaration carries its expectation into every
timeline it is used with.

## The call tree and ordering

Per-binding logs answer questions about one call site; the tape answers
questions about the flow between them. `tape.all` is every event in
recording order, `tape.roots()` is the events with no observed caller,
and `tape.tree()` renders the call graph as it actually ran:

```python
with wrapture.timeline(process, charge, record) as tape:
    place_order("widget")

print(tape.tree())
```

```
Processor.process(order='widget')  -> {'id': 'ch_500', 'amount': 500}
  Gateway.charge(amount=500, currency='USD')  -> {'id': 'ch_500', 'amount': 500}
  Ledger.record(entry={'id': 'ch_500', 'amount': 500})  -> None
```

Each line is one event, indented by nesting depth. A completed call
shows its result after `->`, a call that raised shows `!!` with the
exception type, and a call still in progress shows neither.

`tape.tree(times=True)` appends each timed event's execution time,
and, where observed children account for part of it, its self time,
as `[12.3ms, self 4.1ms]`. The same figure is available for one event
as `tape.self_time(event)`: the event's time minus its observed
children's, which is what separates "slow itself" from "slow because
of what it calls". For a generator the accumulated body time stands
in for the wall duration in both, since the wall figure includes the
consumer's time between yields.

Cross-binding ordering has its own assertion:

```python
tape.assert_order(charge, record)
```

This is a subsequence check, not an exact match: other events may appear
before, between and after, and only the relative order of the given
bindings' events matters. Repeating a binding requires it to have
recorded that many times in that order. On failure the message names
which binding the expectation stalled waiting for and prints the actual
timeline, which reads far better than a list diff.

A step can also be a filtered event log, which is how to say *which*
call: the log is the set of events that count as that step, and the
walk over the tape supplies the order. Bindings and logs mix, and
every filter serves, `with_args()`, `returning()`, `raising()`,
`matching()`:

```python
charge_500 = charge.events.with_args(amount=500)

tape.assert_order(charge_500, charge_500, record.events.with_args(status="failed"))
tape.assert_order(charge.events.raising(TimeoutError), record)
```

Inside the timeline block the logs come from `binding.events`; after
it, `tape.for_binding(charge).with_args(amount=500)` is the same
thing. Two flags tighten the match, each concerned only with the
bindings the steps name (events of any other binding are invisible to
the assertion): `consecutive=True` requires the steps to match a
consecutive run of those bindings' events, nothing of theirs in
between; `exact=True` requires those bindings' events to be exactly
the steps, nothing before or after either, and implies consecutive.
A log step makes its binding's other events visible, so
`assert_order(charge_500, charge_500, exact=True)` also says there
was no charge with any other amount:

```python
tape.assert_order(charge_500, charge_500, record, consecutive=True)
tape.assert_order(charge_500, charge_500, record, exact=True)
```

For the whole-tree counterpart, `wrapture.canonical(tape)` renders a
deterministic fingerprint of the call tree (kinds, paths, nesting and
outcomes, with everything unstable between runs left out) made for
golden-file and snapshot comparisons; see the exporters section of
the ad-hoc tracing page.

## Capturing log messages

A log message the code under test emits is an observation like any
other, and `capture_logs()` records standard library logging onto the
tape as events of kind `"log"`. The capture applies like a binding,
so `timeline()` accepts it alongside them, and its `events` property
speaks the same vocabulary as everything else on this page:

```python
def test_declined_charge_warns():
    charge = wrapture.binding(Gateway, "charge")
    logs = wrapture.capture_logs("myapp.orders")

    with wrapture.timeline(charge, logs):
        place_order(declined_card)

        logs.events.at_level("WARNING").with_message("*declined*").assert_once()
        logs.events.at_level("ERROR").assert_never()
```

`capture_logs(name, level=...)` selects by logger name (an fnmatch
pattern or a list of them, with `exclude=` subtracting) and by level,
a name or a number meaning "at least this severe". The default is
`"WARNING"`, so capture volume is a deliberate choice rather than an
ambient flood; pass `level="DEBUG"` when a test genuinely asserts on
fine detail. Three log-shaped filters join the family on the query
side: `at_level()` (again "at least this severe"), `with_message()`
(case-sensitive fnmatch, so `"*declined*"` finds a substring) and its
negation `without_message()`, with `matching()` as the escape hatch
for anything richer. The captured fields ride in `event.data`:
`level` and `levelno` (both always present, whichever form the
threshold was given in), `message`, `module`, `funcName` and
`lineno`, with the logger name as the event's `path`.

Capture sits at `logging.Logger.handle`, so it hears each record
exactly once, on the logger that emitted it, before propagation and
regardless of handler configuration: no handler is attached, nothing
the application configured is touched, and delivery to the
application's own handlers continues unchanged. Records a logger's
own level suppresses were never emitted and are not captured.

The reason to reach for this over pytest's `caplog` is position: log
events nest inside the call that emitted them, so a test can pin a
message to a specific call rather than to "somewhere during the
test":

```python
def test_retry_warns_inside_the_first_attempt():
    send = wrapture.binding(Publisher, "send")
    logs = wrapture.capture_logs("myapp.publish")

    with wrapture.timeline(send, logs) as tape:
        publish_with_retry(message)

        send.events.assert_times(2)

        warning = logs.events.at_level("WARNING").assert_once().first
        assert tape.parent_of(warning) is send.events.first
```

Ordering assertions mix logs with calls, since `assert_order()`
accepts any event logs as steps:

```python
tape.assert_order(
    send.events,
    logs.events.with_message("*retrying*"),
    send.events,
)
```

A record logged with `logging.exception()` (or `exc_info=True`)
splits cleanly: the message stays the message, msg-percent-args with
no traceback attached, because the familiar message-plus-traceback
blob is a Formatter artifact manufactured on output, not part of the
record. The exception itself lands on the event's `exception` field,
where the existing `raising()` filter finds it:

```python
logs.events.raising(ConnectionError).assert_once()
```

And `tape.tree()` shows messages in place, one line each, the message
repr-escaped so an embedded newline can never break the alignment:

```text
publish_with_retry(message='hi')
  Publisher.send(message='hi')  !! ConnectionError
  log myapp.publish WARNING 'send failed, retrying'
  Publisher.send(message='hi')  -> 'ok'
```

One capture argument exists for what must never be recorded rather
than what a test wants to see: `exclude_message=` names message
patterns that are dropped at capture, before any tape or sink hears
them, the safety valve for messages carrying secrets. Every other
selection by content belongs at query time, as above, where dropping
a message costs nothing but a filter.

## Declaring blocks of code

The third event producer, alongside bindings (calls observed from
outside) and log capture (messages the code emitted), is a
declaration the code makes itself: `wrapture.block(name, data=...)`
is a context manager that records the enclosed stretch of code as
one event of kind `"block"`. The with body's wall time becomes the
event's duration, an exception escaping the body is recorded and
still propagates, `data=` seeds the event's `data` with tags known
at the declaration (the same mapping `binding(data=)` and an observe
entry's `data` table take; anything known only inside the body is
`annotate()`'s job), and everything recorded inside (bound calls, log events, nested blocks)
nests under it. Like a log statement, the marker is embedded by the
author and inert when nothing listens: with no sinks active, nothing
is built at all, so it can stay in production code permanently.

Three options carried over from `binding()` complete the surface.
`when=` takes a plain bool deciding whether this entry records, the
body running either way; unlike a binding, declared once and
consulted per call, a block is entered where the decision can be
made, so the bool is computed beforehand (a `filter_requests()`
filter is evaluated by hand with its `matches()` method).
`tree=True` extends a declined entry to everything beneath it for
the body's whole extent, so an ignored request's inner operations
vanish with it rather than surfacing as parentless roots. And
`stack=` captures how control reached the block, priced exactly as
[on a binding](#capturing-the-call-stack): `"caller"`, a frame
count, or `"full"`, resolved with `stack_frames()`.

Two distinct uses matter in tests. The first is assertable phases in
application code: once the code declares "this is the render phase",
a test asserts on the phase instead of reverse-engineering it from
call patterns. `tape.blocks(name)` selects block events by name (an
fnmatch pattern, like the config filters), returning an `EventLog`
with the whole filter and assertion surface:

```python
def process(invoice):
    with wrapture.block("render-invoice", data={"customer": invoice.customer_id}):
        pages = render(invoice)
        wrapture.annotate(pages=pages)


def test_render_annotates_page_count():
    with wrapture.timeline() as tape:
        process(invoice)

    render = tape.blocks("render-invoice").assert_once().first
    assert render.data["pages"] == 4
```

The context manager yields None; code inside the block reaches the
event through the ambient surface, `annotate()` and
`current_event()`, exactly as anywhere else.

The second use lives in the test body itself: an integration test
that performs several acts otherwise leaves one flat tape, and "the
events during the second request" means parent-chasing. Blocks give
the phases names, and `tape.within(event)` scopes the whole query
surface to one block's contents:

```python
def test_second_request_hits_cache():
    render = wrapture.binding(Renderer, "render")

    with wrapture.timeline(render) as tape:
        with wrapture.block("first request"):
            client.get("/invoice/1")
        with wrapture.block("second request"):
            client.get("/invoice/1")

    second = tape.blocks("second request").assert_once().first
    tape.within(second).for_binding(render).assert_never()
```

The view `within()` returns is tape-like and live: `all`,
`for_binding()`, `blocks()`, `roots()`, `tree()` and
`assert_order()` all work scoped to the block's descendants, so an
ordering assertion on the view never sees an event outside it. The
block event is not a member of its own view (within means contents);
the view exposes it as `.root`, its `roots()` are the block's direct
children, and views nest, `within()` on a view scoping further down.
`within()` works for any event, but a block is the one you name.

Ordering assertions mix blocks with everything else, since
`tape.blocks()` returns an event log and `assert_order()` accepts
event logs as steps; a block takes its position at entry. Blocks of
one name count as one recorder to the `consecutive`/`exact`
strictness flags, the way one binding's calls do. And `tape.tree()`
gains narrative structure for free:

```text
block: first request
  GET /invoice/1 (app)
    Renderer.render(invoice=1)
block: second request
  GET /invoice/1 (app)
```

There is deliberately no decorator form of `block()`. The
whole-function case already has a first-class answer: decorate the
function with `@observed` (or address it from config), and it
records a `"call"` event, which is what a whole function is.
`block()` marks what is smaller than a function, the stretches
inside one that no callable boundary covers.

## Applying instrumentation in a test

An **instrumentation** is the config layer's unit of packaged
patching: a subclass of `wrapture.Instrumentation` whose decorated
hook methods patch its trigger modules when they are imported (the
[ad-hoc tracing guide](ad-hoc-tracing.md#instrumentation-code-that-patches-a-target)
has the contract). Two kinds of test meet it: a test of an
application that runs under an instrumentation, and a test of an
instrumentation package itself.

For the first, `wrapture.instrumentation(...)` scopes an application
of one or more instrumentations to a block. It takes what an
`[[instrument]]` entry's `name` takes (a registered name, a
qualified `name@distribution`, a `module:attr` reference) or the
class itself, and its settings as keyword arguments when there is
one item, or as `(item, settings)` pairs when there are several:

```python
from wrapture_instrumentation_flask import FlaskInstrumentation


def test_every_view_is_observed():
    with wrapture.instrumentation(FlaskInstrumentation, ignore_paths=["/health"]):
        with wrapture.timeline() as tape:
            client.get("/quote/widget")

    assert tape.tree() == ...
```

Triggers already imported (the normal case in a test, where the
framework is imported at the top of the module) apply on entry; any
other applies when its module arrives, and exit removes everything in
reverse and neutralises what never fired. The block yields the same
`AppliedConfig` record a config file produces, so `report()` and
`instrumentations` are available inside it, and it pairs with
`timeline()` exactly as above: the tape is a scoped sink, so the
events the instrumentation's bindings record land on it. The
`triggers=` keyword, valid with a single item like the keyword
settings, scopes the application to a subset of the class's declared
trigger modules, so a multi-trigger class is testable one hook at a
time. Entry refuses, before patching anything, an instrumentation
not removable over the triggers in play (pass
`allow_unremovable=True` to override, for a class you know leaves
nothing behind) and one whose target another applied config already
instruments; and because wrapture keeps one post-import hook per
trigger module process-wide, entering and leaving the scope on every
test of a suite never accumulates hooks for a module that is never
imported.

For the second, the class is built to be tested without wrapture's
hook machinery at all. Construct it (which runs the settings
validation, so a bad setting is a `ConfigError` a test can assert
on), call `apply()` with a trigger name and the imported module, and
call `remove()` afterwards; these are the same base-class methods
wrapture's own dispatch drives, so the direct and deferred paths
cannot behave differently:

```python
import flask


def test_the_instrumentation_applies_and_removes():
    instrumentation = FlaskInstrumentation(capture_headers=True)
    instrumentation.apply("flask.app", flask.app)
    try:
        app = flask.Flask("shop")
        assert isinstance(app.wsgi_app, wrapture.WSGIMiddleware)
    finally:
        instrumentation.remove("flask.app", flask.app)
```

[Instrumentation packages](instrumentation-packages.md)
covers the rest of the author's side: the entry point, the hook
decorator, the import posture, and the bindings recipe for removal.

## Finding a binding applied elsewhere

Everything above asserts through a binding the test itself created:
`charge.events`, `tape.assert_order(charge, record)`. An
instrumentation creates its bindings inside its hook methods, a
config file applies them at startup, application code may patch
itself, and a function decorated with `@wrapture.observed` carries
its recorder inside the wrapper. None of them hands the test a
binding, yet the events they record land on any `timeline()` that is
open. `find_binding()` recovers the handle from the location the
test knows:

```python
def test_failed_charge_is_not_recorded_in_the_ledger():
    charge = wrapture.find_binding(Gateway, "charge")
    record = wrapture.find_binding(label="ledger.record")

    with wrapture.timeline():
        with pytest.raises(TimeoutError):
            place_order("widget")

        charge.events.raising(TimeoutError).assert_once()
        record.events.assert_never()
```

The location is spelled exactly as `binding()` takes it: a module,
class or instance plus attribute steps, or a `"module:path"` string,
with `attr=` or `item=` for a value binding on a slot. It is matched
against each binding's `path`, which is derived from the target and
never affected by a label, so `find_binding(Gateway, "charge")` finds
the binding however its creator spelled the location and whatever
they named it. Nothing is imported or resolved to answer, so a lookup
by string is safe before the module in question has loaded (it just
finds nothing). `label=` instead matches the name a binding shows in
output: its assigned label, or its path when it has none, the same
string a failing assertion's message reports, so
`find_binding(label="shop.gateway:Gateway.charge")` finds an
unlabelled binding. Given together, both have to match. Labels are
matched exactly, not as patterns: a label is an identity.

Only bindings currently applied are found. A binding whose scope has
ended is gone from the results rather than returned stale, so a
lookup after the instrumentation was removed is `NoBindingError`,
not a binding whose `events` then complains for a less obvious
reason. Suspended bindings are included. `observed()` proxies take
part too, for as long as something holds them: an `@observed`
function is found by its label or by its `module:qualname` path.

Two bindings on one target are a supported arrangement (a test's own
`raises` layered over an instrumentation's recorder, say), and then
`find_binding()` refuses to guess: it raises `AmbiguousBindingError`
naming the candidates, and either a label singles one out or
`find_bindings()` returns all of them, in order of application with
the outermost layer last. `find_bindings()` is the plural form
throughout: the same query, a list that may be empty, never an error
for no match.

What comes back is the real binding, not a read-only view, so the
whole surface applies: `events`, `assert_order()` steps,
`is_wrapping()`, `suspend()` and `resume()`, and the behaviour
namespaces. That last point cuts both ways. Configuring
`find_binding(...).on_call.raises(...)` changes what the
instrumentation's own binding does for as long as it stays applied,
which is the power holding the reference always conferred; a test
that wants behaviour of its own should stack a binding through
`timeline(...)` instead, which is removed at exit and leaves the
found binding as it was.

The inverse question, "whose wrapper is this object", is
`binding_of(obj)`: the binding whose wrapper `obj` is, or the
outermost when several are stacked, seeing through later decorators
and bound-method views the way `is_wrapping()` does, or `None` when
nothing in the chain is wrapture's. It answers for a from-import
copy, a callable pulled from a registry, a WSGI or ASGI application
and an `@observed` function (whose recorder is the proxy itself), and
it still recognises the retired wrapper of a binding since removed,
with `removed` saying so. An attribute binding's descriptor is
recognised when read off the class dict, `vars(Owner)["name"]`, not
through the value it returns. `bindings_of(obj)` lists every layer,
outermost first.

When no binding is obtainable at all, or the strings are what the
test has, the tape answers by them directly: `tape.where(path=...)`,
`tape.where(label=...)` and `tape.where(category=...)` return an
`EventLog` selected by the event's path, its display label or its
declared category under the same rules, so the filter and assertion
surface applies, and the result is a step for `assert_order()` like
any other log. `find_binding()` is the usual route, since the
binding is worth more than its events; `where()` is the fallback,
and the way to ask for every external call a test made across
bindings.

## The pytest plugin

wrapture ships an opt-in pytest plugin. It is deliberately not
auto-loaded; activate it from a `conftest.py`:

```python
pytest_plugins = ["wrapture.pytest_plugin"]
```

or on the command line with `-p wrapture.pytest_plugin`. It provides
three things.

**A leak sweep after every test.** A binding the test applied and did
not remove fails that test by name, and is removed so the tests after
it are unaffected. The sweep only flags bindings applied *during* the
test: a module or session scoped fixture that deliberately holds a
patch across tests is respected, because its binding was already
applied when the test began.

**A `tape` fixture.** A recording scope spanning the whole test, so
bindings applied any way at all record onto it:

```python
def test_order_flow(tape):
    with wrapture.binding(Gateway, "charge") as charge:
        place_order("widget")

        charge.events.with_args(amount=500).assert_once()

    assert len(tape.all) == 2
```

When a test that used `tape` fails, the tape's `tree()` is attached to
the failure report under a "wrapture tape" section, so the output shows
what actually ran, not just the assertion that tripped. Note that the
fixture's timeline is given no bindings, so it applies none and
verifies no declared expectations itself; a `bound()` decorator's
binding verifies its own, and for with-block bindings use
`timeline(...)` inside the test where expectations are wanted.

**Assertion output for event logs.** Comparisons involving an
`EventLog` print the events, including the "filtered from" fallback
showing what an over-narrowed filter discarded, matching the output of
the `assert_*` methods.

## Verifying nothing leaked by hand

Without the plugin, an autouse fixture can assert the world was
restored:

```python
import wrapt

@pytest.fixture(autouse=True)
def no_leaked_patch():
    yield
    attr = vars(Gateway)["charge"]
    assert not issubclass(type(attr), wrapt.FunctionWrapper), (
        "a test left Gateway.charge patched"
    )
```

The check reads the raw class attribute with `vars()` and compares types
with `type()` rather than `isinstance()`, because a wrapt wrapper
masquerades as the object it wraps. A different kind of leftover, code
that took a reference to the wrapped callable during a test and calls
it afterwards, records nothing (removal deactivates the wrapper) and
shows as a non-zero `removed_calls` on the binding.
