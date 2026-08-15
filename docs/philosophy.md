# Philosophy

wrapture attaches bindings to call sites in code you do not want to, or
cannot, modify. One mechanism scales from simple monkey patching, through
testing, to tracing a live application. This page explains the thinking
behind that mechanism, and how it relates to `unittest.mock`.

## The code stays real

The foundational choice is to **wrap rather than replace**. A binding
installs a wrapt wrapper around the real callable; unless you configure
behaviour that says otherwise, the real code runs.

That choice has consequences that run through the whole library:

- You can intervene surgically: rewrite one argument, or one field of a
  result, while everything else executes for real.
- Calls an object makes to itself pass through the wrapper too, so nothing
  is structurally invisible the way it is to a substituted double.
- Removing the wrapper restores the original exactly, and the wrapper
  composes with wrappers other parties installed on the same target.

## Loud failure over silent wrongness

A patching tool that misfires quietly produces tests that pass for the
wrong reason. wrapture's API is shaped to make mistakes noisy:

- A misspelled attribute name raises at `binding()` creation, on the line
  that made the mistake, not later as a patch that never fires.
- Mixing the two lifecycle styles raises `AlreadyAppliedError` rather than
  letting an inner scope silently remove a patch an outer scope owns.
- `active` is queried, not cached: if something else replaces or unwraps
  the target behind your back, the binding reports `displaced` instead of
  claiming the patch is in place.
- Calls skipped while suspended are counted on `suspended_calls`, so a
  quiet patch can be told apart from a broken one.

## Declaration is free, effect is explicit

`binding()` declares; it never patches. Bindings can be created at class
or module scope, stored, and reused across apply/remove cycles. Effects
happen only at explicit points: `apply()` installs, `remove()` restores,
`suspend()` disables in place. Each axis is independent, and each is
reversible.

## Built on wrapt, not hiding it

wrapture does not reimplement patching; it adds lifecycle and vocabulary
over wrapt, and it exposes the underlying handle (`bnd.wrapper`,
`bnd.target`, `bnd.name`) so anything core wrapt can do remains available.
If wrapture's API does not cover a case, you can always drop down a level.

## wrapture and unittest.mock

`unittest.mock` substitutes objects: a patched attribute becomes a `Mock`,
and the test asserts against what the `Mock` recorded. wrapture wraps the
real attribute and intervenes in flight. The two overlap on simple cases
and differ sharply once real code needs to keep running.

### Stubbing a return value

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

One difference already shows: with `patch.object` the attribute *is* a
`Mock`, so any other method called on the gateway returns a fabricated
`MagicMock` that supports almost any operation, and a typo in the method
name only fails if spec checking was configured. With wrapture, only the
named method is affected, everything else on the object stays real, and a
typo raises `AttributeError` at creation.

### Injecting a failure

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
the ledger write really does or does not happen, and (once timelines
land, see below) the test can assert on what the rest of the system did
about the failure.

### Running the real code while modifying the call

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

### Seeing calls an object makes to itself

`patch.object(OrderService, "_take_payment")` replaces the method, so the
real payment logic no longer runs. But a `Mock` injected as a collaborator
cannot see `self._take_payment()` at all: the call never crosses the
seam the double sits behind. wrapture wraps the method on the class, so an
internal self-call passes through the wrapper like any other call, with
the real code still running.

### Asserting on what happened

`unittest.mock` records calls on the mock and asserts with
`assert_called_once_with()` and friends. This is the half of mock that
wrapture does not replace today: bindings intervene, but nothing records.

The planned timeline layer adds recording and assertion on top of the same
bindings: events with signature-normalized arguments, real return values
(which mock does not record), nesting and ordering. Until it lands, use
`unittest.mock` where recorded-call assertions are the point of the test.

### When to just use unittest.mock

`unittest.mock` is in the standard library, universally understood, and
the right tool when the code has injectable seams and the test wants a
fabricated collaborator: no real side effects, spec-checked call
signatures, recorded calls. wrapture is not a replacement for it.
wrapture exists for the cases substitution cannot express: code with no
seams, interventions that keep the real code running, and observation of
a real call graph.

```{note}
The testing workflow built on timelines (recording calls, asserting on
arguments, results, ordering and nesting) is designed but not implemented
yet. The comparisons above only show behaviour that works today.
```
