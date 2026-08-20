# Testing code that calls external services

An order service takes payments through a gateway. The gateway is
reached by a client class, an SDK or a thin HTTP wrapper, and every
method on it does network I/O. The service code around it is yours and
is exactly what you want to test: does it record the order correctly,
does it cope when the gateway times out, does cancelling a paid order
actually issue a refund. The gateway itself is what you cannot have in
the test: no network, no sandbox account, no waiting for a real
response.

Substituting the client for a stand-in works until the questions get
more specific: did the service charge the right amount exactly once,
did it refund the right charge, did the client's own validation run?
wrapture leaves the client where it is and intervenes at the one method
that matters: stub the response, inject the timeout, or let the real
method run and adjust one thing about the call, and in every case
record what actually flowed so the test can assert on it. If you are
arriving from `unittest.mock`, the [comparison page](coming-from-mock.md)
maps each idiom you already know onto what is used here.

This example uses: [call behaviour](monkey-patching.md#call-behaviour-changing-what-a-call-does),
[recording on a timeline](unit-testing.md#recording-calls-on-a-timeline),
[filtering and asserting on events](unit-testing.md#filtering-and-asserting-on-events),
[binding groups](monkey-patching.md#binding-groups),
[pytest fixtures](unit-testing.md#scoping-with-pytest-fixtures) and
[the pytest plugin](unit-testing.md#the-pytest-plugin).

## The application code

The client shapes requests and parses responses like any SDK would; the
network edge is one private method, `_post()`, which here refuses to
run at all so that no test can depend on it by accident:

```python
>>> import wrapture

>>> class PaymentClient:
...     def __init__(self, api_key: str) -> None:
...         self.api_key = api_key
...
...     def charge(self, amount: int, currency: str = "USD") -> dict:
...         if amount <= 0:
...             raise ValueError(f"amount must be positive, got {amount}")
...
...         response = self._post("/charges", {"amount": amount, "currency": currency})
...
...         return {"id": response["id"], "amount": amount, "currency": currency}
...
...     def refund(self, charge_id: str) -> dict:
...         response = self._post(f"/charges/{charge_id}/refund", {})
...
...         return {"id": response["id"], "charge": charge_id}
...
...     def _post(self, path: str, payload: dict) -> dict:
...         raise RuntimeError("no network in tests")

```

The service is the code under test. It records each order in memory,
treats a gateway timeout as a recoverable outcome, and refunds on
cancellation only if the order was actually paid:

```python
>>> class OrderService:
...     def __init__(self, client: PaymentClient) -> None:
...         self.client = client
...         self.orders: dict[str, dict] = {}
...
...     def place(self, order_id: str, amount: int) -> dict:
...         try:
...             receipt = self.client.charge(amount)
...         except TimeoutError:
...             order = {"status": "pending", "amount": amount}
...         else:
...             order = {"status": "paid", "amount": amount, "charge_id": receipt["id"]}
...
...         self.orders[order_id] = order
...         return order
...
...     def cancel(self, order_id: str) -> dict:
...         order = self.orders[order_id]
...
...         if order["status"] == "paid":
...             self.client.refund(order["charge_id"])
...
...         order["status"] = "cancelled"
...         return order

>>> service = OrderService(PaymentClient(api_key="sk_test"))
>>> service.place("o-1", 500)
Traceback (most recent call last):
    ...
RuntimeError: no network in tests

```

## The naive approach: swap the client out

The obvious move is to hand the service a fake client with the same
method names that returns whatever the test wants. It works for the
happy path, but the fake has to be kept in step with the real client by
hand, it cannot express "the client's own validation still runs", and
asking it what it was called with means building recording into the
fake yourself. Each of those is something wrapture provides at the real
client instead.

## Stubbing the response, on the class

A binding names the method on the class, not on the instance the service
happens to hold, so it takes effect for every `PaymentClient` the
service constructs, however it constructs it. `returns()` answers the
call with a canned receipt and never reaches the real method, so
`_post()` is never touched:

```python
>>> charge = wrapture.binding(PaymentClient, "charge")
>>> charge.on_call.returns({"id": "ch_TEST", "amount": 500, "currency": "USD"})
<CallBehaviour of <Binding 'PaymentClient.charge' callable unapplied>>

>>> with charge:
...     service.place("o-1", 500)
{'status': 'paid', 'amount': 500, 'charge_id': 'ch_TEST'}

>>> service.orders["o-1"]
{'status': 'paid', 'amount': 500, 'charge_id': 'ch_TEST'}

```

The binding is a context manager, so the patch lasts exactly for the
block. Outside it `charge()` is the real method again, and the order
recorded inside the block is real state in the real service.

The stub answers without running `charge()`, but a call that would not
have fitted `charge()` is still refused, as the real method would
refuse it, so a service that drifts from the client's signature cannot
pass its tests on the strength of the stub:

```python
>>> with charge:
...     service.client.charge(500, memo="gift")
Traceback (most recent call last):
    ...
TypeError: PaymentClient.charge (stubbed): got an unexpected keyword argument 'memo'

```

## Injecting a timeout and checking the service copes

Swap the terminal behaviour to `raises()` and the service sees the
gateway time out. The point of the test is not that the exception is
raised, it is what the service does about it, and that code is real:

```python
>>> charge.on_call.raises(TimeoutError("gateway timed out"))
<CallBehaviour of <Binding 'PaymentClient.charge' callable unapplied>>

>>> with charge:
...     service.place("o-2", 300)
{'status': 'pending', 'amount': 300}

```

Behaviour persists on the binding across applications, so one `charge`
binding can be reconfigured from test to test rather than recreated.

A gateway that is down and then comes back is the more interesting
test, because it exercises both branches of `place()` in one run.
`then(after=2)` adds a second phase that takes over once the first has
handled two calls; the phase has the same verbs as `on_call`, and
nothing carries over from the first, so it starts empty:

```python
>>> recovered = charge.on_call.then(after=2)
>>> recovered.returns({"id": "ch_RETRY", "amount": 300, "currency": "USD"})
<CallPhase 1 of 'PaymentClient.charge'>

>>> with charge:
...     [service.place(f"o-{n}", 300)["status"] for n in (5, 6, 7)]
['pending', 'pending', 'paid']

>>> charge.phase
1

```

The first two orders were placed while the gateway timed out, the third
after it recovered, and `charge.phase` confirms the hand-over
happened. Phases restart on every application, so the same binding
would replay the outage for the next test. This one is done with it:
`reset()` drops every phase and leaves the binding bare.

```python
>>> charge.on_call.reset()
<Binding 'PaymentClient.charge' callable unapplied>

```

## Running the real client code, with one thing changed

Sometimes the client's own logic is part of what the test needs to
exercise: its validation, or the way it shapes a request. Move the stub
down to the network edge, and the real `charge()` runs above it:

```python
>>> post = wrapture.binding(PaymentClient, "_post")
>>> post.on_call.returns({"id": "ch_LIVE_9f2"})
<CallBehaviour of <Binding 'PaymentClient._post' callable unapplied>>

>>> with post:
...     service.place("o-3", 250)
{'status': 'paid', 'amount': 250, 'charge_id': 'ch_LIVE_9f2'}

>>> with post:
...     service.place("o-4", 0)
Traceback (most recent call last):
    ...
ValueError: amount must be positive, got 0

```

The receipt was assembled by the real `charge()`, and the zero amount
was rejected by the client's real validation, which a canned response
would have skipped straight past.

With the real method running, `transforms_args()` and
`transforms_result()` change one side of the call while leaving the
rest of it alone. Here the currency is forced on the way in, and the
gateway's id is pinned to a stable value on the way out, so assertions
do not depend on what a live gateway happened to return:

```python
>>> charge.on_call.transforms_args(lambda args, kwargs: (args, {**kwargs, "currency": "EUR"}))
<CallBehaviour of <Binding 'PaymentClient.charge' callable unapplied>>
>>> charge.on_call.transforms_result(lambda receipt: {**receipt, "id": "ch_TEST"})
<CallBehaviour of <Binding 'PaymentClient.charge' callable unapplied>>

>>> with post, charge:
...     service.client.charge(250)
{'id': 'ch_TEST', 'amount': 250, 'currency': 'EUR'}

```

The `reset()` earlier left the binding bare, so the transforms compose
around the real call rather than around a leftover `raises()`.

## Recording what the service did to the gateway

Everything so far changed the call. To check that the service *made*
the right calls, open a timeline. The bindings given to `timeline()`
are applied for the block and every call through them lands on the tape
as an event, with real arguments, real results and real nesting:

```python
>>> charge.on_call.passes_through()
<CallBehaviour of <Binding 'PaymentClient.charge' callable unapplied>>
>>> refund = wrapture.binding(PaymentClient, "refund")

>>> with wrapture.timeline(charge, refund, post) as tape:
...     _ = service.place("o-5", 700)
...     _ = service.cancel("o-5")
...     print(tape.tree())
PaymentClient.charge(amount=700, currency='USD')  -> {'id': 'ch_LIVE_9f2', 'amount': 700, 'currency': 'USD'}
  PaymentClient._post(path='/charges', payload={'amount': 700, 'currency': 'USD'})  -> {'id': 'ch_LIVE_9f2'} (injected)
PaymentClient.refund(charge_id='ch_LIVE_9f2')  -> {'id': 'ch_LIVE_9f2', 'charge': 'ch_LIVE_9f2'}
  PaymentClient._post(path='/charges/ch_LIVE_9f2/refund', payload={})  -> {'id': 'ch_LIVE_9f2'} (injected)

```

Only `_post` carries behaviour here; `charge` and `refund` are on the
timeline purely to be watched, and the tree marks the injected result
so a stubbed value is never mistaken for a real one.

Assertions go through each binding's `events`, a filtered view of the
tape for that one call site, and work inside the block once the code
under test has run. `with_args()` matches on the normalized arguments,
so it does not matter whether the service passed the amount
positionally or by name. The same filtered logs serve as the steps of
`tape.assert_order()`, here saying the refund came after the charge
it refers to:

```python
>>> with wrapture.timeline(charge, refund, post) as tape:
...     _ = service.place("o-6", 700)
...     _ = service.cancel("o-6")
...     charged = charge.events.with_args(amount=700)
...     refunded = refund.events.with_args(charge_id="ch_LIVE_9f2")
...     _ = charged.assert_once()
...     _ = refunded.assert_once()
...     _ = post.events.assert_times(2)
...     _ = tape.assert_order(charged, refunded)

```

The same recording answers the negative question. Cancelling an order
that never got paid must not refund anything, and `assert_never()` says
so, printing the events it looked at if it is wrong:

```python
>>> charge.on_call.raises(TimeoutError("gateway timed out"))
<CallBehaviour of <Binding 'PaymentClient.charge' callable unapplied>>

>>> with wrapture.timeline(charge, refund):
...     _ = service.place("o-7", 400)
...     _ = service.cancel("o-7")
...     _ = charge.events.raising(TimeoutError).assert_once()
...     _ = refund.events.assert_never()

```

## Declaring the expectation up front

An assertion has to be remembered at the end of the block. An
expectation is declared on the binding before the block and verified
automatically when the timeline exits, which suits the invariants a
test always wants checked: one charge per order, one refund per
cancelled paid order:

```python
>>> charge.on_call.passes_through()
<CallBehaviour of <Binding 'PaymentClient.charge' callable unapplied>>
>>> _ = charge.expect_once()
>>> _ = refund.expect_once()

>>> with wrapture.timeline(charge, refund, post):
...     _ = service.place("o-8", 900)
...     _ = service.cancel("o-8")

```

Nothing printed, because both expectations held. When one does not, the
timeline raises `ExpectationNotMetError` on exit, an `AssertionError`
subclass, listing the events it found. Suppose the gateway times out on
the charge and the order is later cancelled: the service correctly
refunds nothing, because nothing was paid, and the declared expectation
of one refund reports the mismatch without a line of assertion in the
block:

```python
>>> charge.on_call.raises(TimeoutError("gateway timed out"))
<CallBehaviour of <Binding 'PaymentClient.charge' callable unapplied>>

>>> with wrapture.timeline(charge, refund, post):
...     _ = service.place("o-9", 900)
...     _ = service.cancel("o-9")
Traceback (most recent call last):
    ...
wrapture.exceptions.ExpectationNotMetError: declared expectation on PaymentClient.refund not met: expected exactly 1 event(s), got 0
<EventLog PaymentClient.refund: 0 event(s)>
    (no events)

```

Expectations persist on the binding like behaviour does, so a binding
declared once at module scope carries its expectation into every test
that uses it; the next section starts from fresh bindings for that
reason.

## Grouping the client's methods

Two or three bindings on the same client soon want to move together.
`bindings()` declares them as one group with one lifecycle, each member
carrying its own behaviour, so a "gateway offline" scenario is one
object a test enters:

```python
>>> offline = wrapture.bindings(charge=wrapture.binding(PaymentClient, "charge"),
...                             refund=wrapture.binding(PaymentClient, "refund"))
>>> _ = offline.charge.on_call.raises(TimeoutError("gateway timed out"))
>>> _ = offline.refund.on_call.raises(TimeoutError("gateway timed out"))

>>> with wrapture.timeline(offline):
...     _ = service.place("o-10", 100)
...     _ = offline.charge.events.raising(TimeoutError).assert_once()
...     service.orders["o-10"]
{'status': 'pending', 'amount': 100}

```

A group passes straight to `timeline()`, and members are reached by
attribute for configuring behaviour and reading events. When the aim is
to watch every public method of the client rather than name each one,
`wrapture.discover(PaymentClient, "*", exclude="_*")` builds the same
kind of group by pattern; see
[discovering members by pattern](unit-testing.md#discovering-members-by-pattern).

## As a pytest suite

In a test file the same moves become fixtures. A yield fixture applies
the binding for one test and removes it however the test ends, and
because it yields the binding, a test that needs a different response
reconfigures it in place:

```python
import pytest
import wrapture


@pytest.fixture
def gateway():
    charge = wrapture.binding(PaymentClient, "charge")
    charge.on_call.returns({"id": "ch_TEST", "amount": 500, "currency": "USD"})

    with charge:
        yield charge


@pytest.fixture
def service():
    return OrderService(PaymentClient(api_key="sk_test"))


def test_paid_order_records_the_charge(gateway, service):
    assert service.place("o-1", 500)["charge_id"] == "ch_TEST"


def test_timeout_leaves_the_order_pending(gateway, service):
    gateway.on_call.raises(TimeoutError("gateway timed out"))

    assert service.place("o-2", 300)["status"] == "pending"


@wrapture.bound(PaymentClient, "_post").on_call.returns({"id": "ch_LIVE_9f2"})
@wrapture.bound(PaymentClient, "refund").expect_once()
def test_cancelling_a_paid_order_refunds_it(service, _post, refund):
    service.place("o-3", 700)
    service.cancel("o-3")

    refund.events.with_args(charge_id="ch_LIVE_9f2").assert_once()
```

The first two tests share their binding through a fixture, which is the
right home for configuration several tests want. The third binds two
targets for itself alone, and uses the decorator form instead: each
`wrapture.bound()` line addresses a target exactly as `binding()`
would, applies a fresh binding around the test, and injects it under
the slot's name, with the plugin's `tape` fixture recording as usual.
The `expect_once()` declaration is the same up-front expectation shown
earlier, verified when the decorator removes the binding after a
passing test.
[Scoping with decorators](unit-testing.md#scoping-with-decorators)
covers the form.

Keep fixtures that apply bindings function scoped, the pytest default,
so no patch outlives the test that wanted it. wrapture's pytest plugin
backs that up. Enable it once in `conftest.py`:

```python
pytest_plugins = ["wrapture.pytest_plugin"]
```

Every test is then swept for a binding it applied and did not remove,
failing that test by name rather than letting the patch bleed into the
next one. The plugin also provides a `tape` fixture spanning the whole
test and attaches its call tree to the failure report, so a failing
order test shows what the service actually asked the gateway, not just
which assertion tripped.

## Where next

[Call behaviour](monkey-patching.md#call-behaviour-changing-what-a-call-does)
covers the full vocabulary, including `validates_args()` and
`decorates()` for what a transform cannot express, such as running the
real call and then raising to simulate a lost response.
[Recording calls on a timeline](unit-testing.md#recording-calls-on-a-timeline)
explains what each event holds, including the `forwarded` arguments the
real method received after a transform, and
[filtering and asserting on events](unit-testing.md#filtering-and-asserting-on-events)
lists every filter, assertion and declared expectation. For the
suite-level patterns, see
[scoping with pytest fixtures](unit-testing.md#scoping-with-pytest-fixtures)
and [the pytest plugin](unit-testing.md#the-pytest-plugin).
