# Getting started

wrapture is on PyPI. Add it to a project:

```console
$ uv add wrapture
```

or, to just try it, start an interpreter with wrapture temporarily
available, no project or virtual environment to set up or clean up:

```console
$ uv run --with wrapture python
```

Everything on this page can be pasted into that interpreter, line by
line.

## Your first binding

A binding names one attribute of a class or module, and holds behaviour
to apply at that call site. Define something to bind to:

```python
>>> import wrapture

>>> class Gateway:
...     def charge(self, amount, currency="USD"):
...         return {"id": f"ch_{amount}", "amount": amount}

>>> charge = wrapture.binding(Gateway, "charge")
>>> charge
<Binding '__main__:Gateway.charge' callable unapplied>

```

Creating a binding never patches anything, and configuring behaviour on
it does not either. The binding is an ordinary object you hold, and its
repr always tells you its state:

```python
>>> charge.on_call.returns({"id": "stub"})
<CallBehaviour of <Binding '__main__:Gateway.charge' callable unapplied>>

>>> gateway = Gateway()
>>> gateway.charge(500)
{'id': 'ch_500', 'amount': 500}

```

Still the real method: nothing is applied yet. `apply()` installs the
patch, and from then on the configured behaviour answers:

```python
>>> charge.apply()
<Binding '__main__:Gateway.charge' callable active>
>>> gateway.charge(500)
{'id': 'stub'}

```

A binding can be made inert without being removed, which is safe to do
even while other code is calling the method:

```python
>>> charge.suspend()
<Binding '__main__:Gateway.charge' callable active suspended>
>>> gateway.charge(500)
{'id': 'ch_500', 'amount': 500}
>>> charge.resume()
<Binding '__main__:Gateway.charge' callable active>

```

And removing it restores the original exactly:

```python
>>> charge.remove()
<Binding '__main__:Gateway.charge' callable unapplied>
>>> gateway.charge(500)
{'id': 'ch_500', 'amount': 500}

```

For the common case of a patch scoped to a block, a binding is a
context manager, and behaviour reads naturally in one line:

```python
>>> with wrapture.binding(Gateway, "charge").on_call.raises(TimeoutError("down")):
...     gateway.charge(500)
Traceback (most recent call last):
    ...
TimeoutError: down

>>> gateway.charge(500)
{'id': 'ch_500', 'amount': 500}

```

Behaviour is not limited to stubbing and failing. The real method can
keep running while one thing about the call is changed, which is the
case substitution-based tools cannot express:

```python
>>> pinned = wrapture.binding(Gateway, "charge")
>>> pinned.on_call.transforms_result(lambda r: {**r, "id": "ch_TEST"})
<CallBehaviour of <Binding '__main__:Gateway.charge' callable unapplied>>

>>> with pinned:
...     gateway.charge(500)
{'id': 'ch_TEST', 'amount': 500}

```

The real `charge()` ran; only the result's id was rewritten on the way
out.

## Your first recording

Bindings also observe. Build a small call graph:

```python
>>> class Ledger:
...     def record(self, entry):
...         return f"led_{entry['id']}"

>>> class OrderService:
...     def __init__(self):
...         self.gateway = Gateway()
...         self.ledger = Ledger()
...     def place(self, amount):
...         result = self.gateway.charge(amount)
...         self.ledger.record(result)
...         return result

```

`timeline()` opens a recording scope: bindings passed to it are applied
on entry and removed on exit, and every call through them lands on the
tape as an event, nested the way the calls actually nested:

```python
>>> place = wrapture.binding(OrderService, "place")
>>> charge = wrapture.binding(Gateway, "charge")
>>> record = wrapture.binding(Ledger, "record")

>>> with wrapture.timeline(place, charge, record) as tape:
...     _ = OrderService().place(500)
...     print(tape.tree())
__main__:OrderService.place(amount=500)  -> {'id': 'ch_500', 'amount': 500}
  __main__:Gateway.charge(amount=500, currency='USD')  -> {'id': 'ch_500', 'amount': 500}
  __main__:Ledger.record(entry={'id': 'ch_500', 'amount': 500})  -> 'led_ch_500'

```

That is the call graph as it ran: arguments normalized against the real
signatures, real return values, nesting from actual causality. No code
in `OrderService`, `Gateway` or `Ledger` was written with observation
in mind.

Events are queried and asserted on through each binding, inside the
timeline:

```python
>>> with wrapture.timeline(place, charge, record) as tape:
...     _ = OrderService().place(500)
...     _ = charge.events.with_args(amount=500).assert_once()
...     _ = record.events.raising().assert_never()
...     charge.events.first.result
{'id': 'ch_500', 'amount': 500}

```

Assertions follow one rule everywhere: methods starting with `assert_`
raise on failure, and everything else returns data. A failed assertion
prints the events it looked at, so the failure output shows what
actually happened.

## Your first pytest test

The same two moves, a behaviour patch and a recording, as a test file.
Save this as `test_orders.py`; it is self-contained:

```python
import wrapture


class Gateway:
    def charge(self, amount, currency="USD"):
        return {"id": f"ch_{amount}", "amount": amount}


class OrderService:
    def __init__(self):
        self.gateway = Gateway()

    def place(self, amount):
        return self.gateway.charge(amount)


def test_charge_is_stubbed():
    with wrapture.binding(Gateway, "charge").on_call.returns({"id": "stub"}):
        assert OrderService().place(500) == {"id": "stub"}


def test_what_flowed_through():
    charge = wrapture.binding(Gateway, "charge")

    with wrapture.timeline(charge):
        OrderService().place(500)

        charge.events.with_args(amount=500).assert_once()
```

and run it the same no-setup way:

```console
$ uv run --with wrapture --with pytest pytest test_orders.py
```

For a real suite, wrapture ships an opt-in pytest plugin that sweeps
every test for patches left applied and attaches recordings to failure
reports; one line in `conftest.py` enables it:

```python
pytest_plugins = ["wrapture.pytest_plugin"]
```

## Where next

- Coming from `unittest.mock`? The comparison page maps each mock idiom
  to its wrapture counterpart, and says when mock remains the right
  tool.
- The monkey patching page is the full reference for bindings:
  behaviour, lifecycle, groups, attribute bindings, iterators.
- The unit testing page covers the recording workflow in depth:
  scoping patterns, event filters and assertions, expectations, capture
  policies, and the pytest plugin.
- The design philosophy page explains the thinking: why wrapping beats
  substitution, and why the API fails loudly.
