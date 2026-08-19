# Coming from unittest.mock

`unittest.mock` substitutes objects: a patched attribute becomes a
`Mock`, and the test asserts against what the `Mock` recorded. wrapture
wraps the real attribute and intervenes in flight. The two overlap on
simple cases and differ sharply once real code needs to keep running.

The quick translation, expanded below:

| unittest.mock | wrapture |
|---|---|
| `patch.object(C, "m", return_value=v)` | `binding(C, "m").on_call.returns(v)` |
| `patch.object(C, "m", side_effect=exc)` | `binding(C, "m").on_call.raises(exc)` |
| `patch.object(C, "m", autospec=True, ...)` | the default; `strict=False` to opt out |
| `side_effect=fn` (fabricate per call) | `on_call.decorates(fn)` |
| `side_effect=[a, b, exc]` (a sequence) | `on_call.returns_from([a, b])`, then `then().raises(exc)` |
| `Mock(wraps=real)` | `on_call.transforms_args(fn)` / `transforms_result(fn)` / `decorates(fn)` |
| `patch.multiple(C, a=..., b=...)` | `bindings(a=(C, "a"), b=(C, "b"))`, per-member behaviour |
| `m.assert_called_once_with(a=1)` | `events.with_args(a=1).assert_once()` |
| `m.call_args_list` | `events` (filterable) or `tape.all` |
| `m.assert_not_called()` | `events.assert_never()` or `expect_never()` |

## Stubbing a return value

Both tools do this well, and for a pure stub there is little to choose
between them:

```python
# unittest.mock
from unittest.mock import patch

with patch.object(Gateway, "charge", return_value={"id": "stub"}):
    order_service.place("widget", 1)
```

```python
# wrapture
import wrapture

with wrapture.binding(Gateway, "charge").on_call.returns({"id": "stub"}):
    order_service.place("widget", 1)
```

One of mock's habits deserves a warning even here. A `MagicMock`
fabricates attributes on demand, so the moment the substitution is any
wider than one method (patching the class itself, or injecting `Mock()`
as a collaborator), every method on the fabricated object succeeds with
a fabricated result, and a misspelled method call passes silently unless
spec checking was configured. wrapture has no fabricating object
anywhere: a binding names one real attribute, a misspelled name raises
`AttributeError` at creation, and everything the binding does not
explicitly change stays the real code.

The same goes for the call itself. mock validates a stubbed call's
arguments against the real signature only if `autospec=True` (or
`create_autospec`) was asked for; without it, `charge(500, bogus=True)`
returns the stub happily and the drift shows up in production. A
wrapture binding is strict by default: a call that `returns()`,
`raises()` or `decorates()` would answer without reaching the real
method is bound to the method's signature first and raises `TypeError`
as the real call would. `binding(..., strict=False)` turns that off for
the rare patch that means to accept a different shape.

## Injecting a failure

Again equivalent on the surface:

```python
# unittest.mock
with patch.object(Gateway, "charge", side_effect=TimeoutError("down")):
    ...
```

```python
# wrapture
with wrapture.binding(Gateway, "charge").on_call.raises(TimeoutError("down")):
    ...
```

In both versions the patched method itself never runs: the call raises
instead, exactly as with mock's `side_effect`. The difference is
everything around the failure. In the mock version the rest of the
pipeline is typically also mocked, so the test can only assert that the
exception propagated. With wrapture only the one bound method is replaced
and the rest of the pipeline stays real: the reservation is really taken,
the ledger write really does or does not happen, and by recording on a
timeline the test can assert on what the rest of the system did about
the failure.

## A sequence of outcomes

mock's `side_effect` also takes a list, consumed one entry per call,
with exceptions raised and anything else returned:

```python
# unittest.mock
with patch.object(Gateway, "charge", side_effect=[{"id": "A"}, TimeoutError("down"), {"id": "B"}]):
    ...
```

wrapture keeps values and exceptions apart. `returns_from()` hands out
successive values from an iterable, lazily, and `then()` adds a phase
that takes over, here once the sequence is exhausted; each phase has
the full behaviour vocabulary, so the third outcome is another phase
in turn:

```python
# wrapture
charge = wrapture.binding(Gateway, "charge")
charge.on_call.returns_from([{"id": "A"}])

down = charge.on_call.then()
down.raises(TimeoutError("down"))

back = down.then(after=1)
back.returns({"id": "B"})
```

More lines for the same three outcomes, but each phase says what it is,
and the same shape covers what a list cannot: `then(after=n)` and
`then(until=fn)` change behaviour on a count or on a condition seen in
the calls, `advance()` moves on from the test, and `binding.phase` and
`in_phase(n)` on the recording tell you which regime a call ran under.
[Phased behaviour](monkey-patching.md#phased-behaviour-changing-what-a-call-does-over-time)
has the details.

## Patching several methods at once

Mock does this too: stack the context managers, or use
`patch.multiple()`. Each patched attribute becomes its own `Mock`,
configured separately:

```python
# unittest.mock
with (
    patch.object(Gateway, "charge", return_value={"id": "stub"}),
    patch.object(Gateway, "refund", side_effect=TimeoutError("down")),
):
    ...
```

With wrapture, several bindings are a group: one object, one lifecycle,
each member carrying its own behaviour, and the members need not even be
on the same class:

```python
# wrapture
group = wrapture.bindings(charge=(Gateway, "charge"),
                          refund=(Gateway, "refund"),
                          record=(Ledger, "record"))
group.charge.on_call.returns({"id": "stub"})
group.refund.on_call.raises(TimeoutError("down"))

with group:
    ...
```

Two things here have no mock equivalent. The `record` member has no
behaviour at all: it stays the real method and is there to be observed,
so inside a timeline the group mixes stubbed, failing, and
purely-watched methods in one declaration. And the group never
half-applies: if any member fails to apply, the ones already applied
are removed again, where a stack of `patch.object` managers that fails
midway unwinds only through the ordinary context manager machinery.
Because bindings wrap rather than replace, several bindings can also
stack on the *same* method, composing with wrappers other parties
installed.

## Running the real code while modifying the call

This is where substitution runs out of road. `Mock(wraps=real)` forwards
calls but cannot modify the arguments the original receives, cannot
post-process its result, and `create_autospec(spec, wraps=real)` accepts
`wraps` and ignores it. The stdlib has no way to say "run the real method,
but change one thing".

With wrapture this is the ordinary case:

```python
# run the real charge, but pin its result id for stable assertions
with wrapture.binding(Gateway, "charge").on_call.transforms_result(
    lambda r: {**r, "id": "ch_TEST"}
):
    ...

# run the real charge, but force the sandbox currency on the way in
with wrapture.binding(Gateway, "charge").on_call.transforms_args(
    lambda args, kwargs: (args, {**kwargs, "currency": "EUR"})
):
    ...
```

## Seeing calls an object makes to itself

`patch.object(OrderService, "_take_payment")` replaces the method, so the
real payment logic no longer runs. But a `Mock` injected as a collaborator
cannot see `self._take_payment()` at all: the call never crosses the
seam the double sits behind. wrapture wraps the method on the class, so an
internal self-call passes through the wrapper like any other call, with
the real code still running.

## Asserting on what happened

`unittest.mock` records calls on the mock and asserts with
`assert_called_once_with()` and friends: a flat call list, arguments by
reference, no return values. wrapture records on a timeline, through the
same bindings that intervene:

```python
with wrapture.timeline(charge, record):
    place_order("widget")

    charge.events.with_args(amount=500).assert_once()
    record.events.raising(TimeoutError).assert_never()
```

Events carry signature-normalized arguments, real return values (which
mock does not record), exceptions, nesting and ordering, and the same
handle asserts on them. Two habits transfer directly, upgraded: argument
matching is by parameter name against the normalized call, so
`with_args(amount=500)` matches however the caller spelled it; and where
mock's misspelled `assert_calld_once` famously passed silently for
years, a misspelled wrapture assertion is an `AttributeError`.

The unit testing page covers the workflow: filters, assertions,
declared expectations, the call tree, and the pytest plugin.

## When to just use unittest.mock

`unittest.mock` is in the standard library, universally understood, and
the right tool when the code has injectable seams and the test wants a
fabricated collaborator: no real side effects, spec-checked call
signatures, recorded calls. wrapture is not a replacement for it.
wrapture exists for the cases substitution cannot express: code with no
seams, interventions that keep the real code running, and observation of
a real call graph.
