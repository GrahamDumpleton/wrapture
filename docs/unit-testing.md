# Using wrapture in tests

Bindings are ordinary objects with an explicit lifecycle, so they fit any
test framework's scoping tools without special integration. What matters
in a test suite is that every applied binding is removed again, whatever
the outcome of the test: a patch that leaks changes the behaviour of every
test that runs after it.

This page shows the scoping patterns for plain tests, pytest and unittest,
then the recording layer built on timelines: how to capture what actually
happened inside a test as events, and how to filter and assert on them.

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
    group = wrapture.bindings(charge=(Gateway, "charge"),
                              refund=(Gateway, "refund"))
    group.charge.on_call.raises(TimeoutError("down"))
    group.refund.on_call.raises(TimeoutError("down"))

    with group:
        ...
```

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
  reset with `passes_through()`.
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
context manager themselves.

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
    assert outer.label == "Gateway.charge"
    assert inner.label == "Ledger.record"
    assert inner.parent is outer
```

`timeline()` accepts bindings, binding groups, or iterables of either, and
with no arguments it only records, for bindings whose lifetime is managed
elsewhere. A binding applied outside any timeline records nothing and
costs almost nothing beyond wrapt's own dispatch, so leaving bindings
applied while only occasionally recording is a supported pattern, not a
mistake.

### What one event contains

Every call through a binding inside the scope records one event. The
fields a test typically reads:

- `path` is the fully qualified location of what was bound, in
  `module:path` form with both halves dotted (the convention setuptools
  entry points use), so two same-named classes in different modules stay
  distinguishable and the event remains self-describing if it ever
  leaves the process. `label` is the friendly name: the `label=` given
  to the binding, or an `owner.name` default like `Gateway.charge`.
  Assert against `label` for readability, `path` when location matters.
- `instance` is the object the method was called on.
- `args` and `kwargs` are the call as the caller wrote it, and
  `arguments` is the signature-normalized form with defaults applied, so
  `charge(500)` and `charge(amount=500)` record identically. Assert
  against `arguments`.
- `result` holds the return value, or the wrapt MISSING sentinel when the
  call raised instead; `exception` holds the exception that propagated. A
  call that returned None records `result=None`, which stays
  distinguishable from no result at all.
- `parent`, `children` and `depth` place the event in the call tree, and
  `seq` is its position in overall recording order.

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

### What does not record

Three situations produce no event, each deliberate:

- **Outside a timeline** nothing records, but configured behaviour still
  applies: a stub is a stub whether or not anyone is recording.
- **While suspended** a binding records nothing, but counts the calls it
  skipped in `suspended_calls`, so a shorter-than-expected tape can be
  explained rather than silently wrong.
- **Calls triggered by the recording machinery itself** are not recorded,
  which is what keeps an observed callable safe to use anywhere, even
  inside the recorder. Behaviour still applies to such calls: code
  stubbed out stays stubbed out.

One honest caveat while the layer is under construction: a call that
returns a generator records the generator object as its result, with
iteration not yet observed.

### How much is captured

By default, values are recorded *by reference*, which is exactly what
`unittest.mock` does: free, and accurate as long as nothing mutates the
value after the call. Code that does mutate its inputs makes a
by-reference record lie retroactively, so capture is a policy, set per
binding:

```python
record = wrapture.binding(Ledger, "record", capture=wrapture.SUMMARY)
```

The levels, ordered by cost:

- `NONE` records the event but no values: the call stays visible, its
  arguments and result do not, and the signature binding that dominates
  recording cost is skipped entirely.
- `TYPES` stores type names only (`<list>`), and never calls user code.
- `REFERENCE` stores references; the default.
- `SUMMARY` stores a bounded, type-aware repr. It survives locks and
  sockets and retains nothing, but repr is user code and may have side
  effects: summarising a lazy ORM object can issue the very query being
  observed.
- `SNAPSHOT` deep-copies for full fidelity, falling back to `SUMMARY`
  for values that refuse (locks, sockets, connections) rather than
  failing the call under test.

Arguments and results are separate axes, since one is a time cost and
the other a retention cost: `capture=` sets both, and `capture_args=` /
`capture_result=` override it individually. A policy can also be a
callable `fn(name, value)`, and `redact()` builds a common one:

```python
charge = wrapture.binding(Gateway, "charge",
                          capture_args=wrapture.redact("card_number"))
```

Redaction matches by parameter name against the normalized arguments,
so positional and keyword calls redact identically. Results have no
parameter name, so pair it with `capture_result=NONE` when the secret
comes back out.

A result supplied by `returns()`, `raises()` or `rejects()` is recorded,
not suppressed, because the stubbed value is precisely what flowed
downstream; the event is marked instead. `tree()` renders the mark as
`(injected)`, and `events.injected()` / `events.injected(False)` filter
on it.

### Annotation

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

### One naming rule

> A method whose name starts with `assert_` raises, immediately. One
> starting with `expect_` declares, and is checked when the timeline
> exits. Everything else returns data.

The rule holds on every object in the package, so a single line read out
of context still says whether and when it can fail a test. A mistyped
assertion name is an `AttributeError`, never a silent pass.

### Filters narrow, and never raise

Each filter returns a new, narrowed `EventLog`, so filters chain freely:

- `of_kind("call", "get", ...)` narrows by event kind.
- `matching(predicate)` keeps events the predicate accepts.
- `raising(TimeoutError)` keeps events that raised one of the given
  exception types; `raising()` keeps events that raised anything.
- `with_args(amount=500)` keeps calls whose *normalized* arguments
  include the given values, so `with_args(currency="USD")` also matches
  calls that relied on the default.
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

### Asserting

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
timeline verifies the bindings (and group members) it was given.
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

## Verifying nothing leaked

For suite-wide insurance, an autouse fixture can assert the world was
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
masquerades as the object it wraps.

This is deliberately manual for now. A pytest plugin that sweeps for
bindings left applied at the end of each test is planned.
