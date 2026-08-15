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

Two honest caveats while the layer is under construction: recorded
arguments and results are references, so an object mutated after the call
shows its mutated state on the tape; and a call that returns a generator
records the generator object as its result, with iteration not yet
observed. Both are being addressed in later stages of this layer.

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

> A method whose name starts with `assert_` raises on failure.
> Everything else returns data.

The rule holds on every object in the package, so a single line read out
of context still says whether it can fail a test. A mistyped assertion
name is an `AttributeError`, never a silent pass.

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
