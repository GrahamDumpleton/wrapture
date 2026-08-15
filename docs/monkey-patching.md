# Monkey patching

wrapture provides a lifecycle and a behaviour vocabulary over wrapt's
monkey-patching machinery. You name a target attribute, configure what
should happen when it is used, and apply the patch. Removing it restores
the original.

## Creating a binding

A binding names one attribute of a module, class or instance:

```python
import wrapture

charge = wrapture.binding(Gateway, "charge")
```

`target` is a module, class, instance, or a string naming a module. `name`
is a dotted path to the attribute:

```python
wrapture.binding(myapp.gateway, "Gateway.charge")
wrapture.binding("myapp.gateway", "Gateway.charge")
```

Creating a binding does not patch anything. Declaring bindings at class or
module scope is safe; the patch is only installed when you ask for it.

A misspelled attribute name raises `AttributeError` at creation, on the
line that made the mistake. This check is a side effect of mode detection
(see [Modes](#modes)), so it has two exceptions. Passing an explicit
`mode=` skips detection, and a typo in such a binding only surfaces when
`apply()` resolves the target. Passing `missing_ok=True` deliberately
accepts a name that does not resolve, to allow binding a name that
genuinely is not defined on the class, typically one assigned in
`__init__`; a typo is then accepted too.

The target module must already be imported. wrapt's trailing `?` syntax for
deferred patching is rejected with `DeferredTargetError`; import the module
first and bind against it.

## Applying and removing

`apply()` installs the wrapper; `remove()` uninstalls it and restores the
original:

```python
charge = wrapture.binding(Gateway, "charge").on_call.returns({"id": "stub"})
charge.apply()
...
charge.remove()
```

`apply()` also returns the binding, so it can be chained onto creation
where the compactness is wanted.

`remove()` is idempotent, and a removed binding can be applied again. For a
scoped patch, use the binding as a context manager instead:

```python
with wrapture.binding(Gateway, "charge").on_call.returns({"id": "stub"}):
    ...
# removed again here
```

The two styles must not be mixed: calling `apply()` on an already applied
binding raises `AlreadyAppliedError`.

Three properties report state honestly:

```python
charge.applied     # did we install the wrapper
charge.active      # is it still installed on the target (queried, not cached)
charge.suspended   # is an applied wrapper currently inert
```

`active` resolves the target and inspects the wrapper chain on every
access, so if a third party replaces the attribute wholesale, or removes
the patch behind your back, the binding reports it. `repr(charge)` shows one
of three states: `unapplied`, `active` or `displaced`.

## Behaviour

Behaviour is configured through the `on_call` namespace. Every method
returns the binding, so configuration chains with `apply()`.

### Substituting results and failures

```python
charge.on_call.returns(value)      # return value; the real callable never runs
charge.on_call.raises(exc)         # raise exc; the real callable never runs
```

Both replace the call outright: the wrapped callable is never invoked,
matching what `unittest.mock` does with `return_value` and `side_effect`.
To run the real callable and raise afterwards, for example to simulate a
response lost after the operation actually succeeded, use `decorates()`,
which controls both sides of the call:

```python
def charge_then_drop(wrapped, instance, args, kwargs):
    wrapped(*args, **kwargs)
    raise TimeoutError("response lost")

charge.on_call.decorates(charge_then_drop)
```

The return value of `wrapped()` is deliberately ignored here: the caller
is going to see the raised exception instead, so there is nothing to pass
it back to. The real call still happened, which is the point, and its side
effects stand.

### Wrapping with a decorator

`decorates()` takes a function with wrapt's decorator signature, so an
existing `@wrapt.decorator` function can be passed directly:

```python
def around(wrapped, instance, args, kwargs):
    kwargs.setdefault("currency", "EUR")
    return wrapped(*args, **kwargs)

charge.on_call.decorates(around)
```

It decides whether and how the real callable is invoked.

### Transforming and validating

```python
charge.on_call.transforms_args(fn)      # fn(args, kwargs) -> (args, kwargs)
charge.on_call.transforms_result(fn)    # fn(result) -> result
charge.on_call.validates_args(check)    # check(*args, **kwargs); call unchanged
charge.on_call.validates_result(check)  # check(result); result unchanged
```

A validation check that raises fails the call; anything it returns is
ignored.

### The behaviour pipeline

Configured behaviour forms a pipeline rather than a single slot:

- `transforms_*` and `validates_*` are **composing** stages. They wrap
  around what follows and accumulate in the order added.
- `returns`, `raises` and `decorates` are **terminal**. They decide what
  happens at the centre, and setting a new terminal replaces the previous
  one while composing stages persist.

```python
charge = wrapture.binding(Gateway, "charge")
charge.on_call.transforms_args(lambda a, k: ((a[0] * 100,), k))
charge.on_call.transforms_result(lambda r: {**r, "sandbox": True})
charge.apply()
```

`passes_through()` drops all configured behaviour, both the terminal and
every composing stage, while leaving the patch installed:

```python
charge.on_call.passes_through()
```

### Reconfiguring a live binding

Behaviour can be set before or after `apply()`, and changed at any time
while the patch is installed:

```python
charge = wrapture.binding(Gateway, "charge").apply()
charge.on_call.returns({"id": "A"})
...
charge.on_call.returns({"id": "B"})
...
charge.remove()
```

### Async targets

Result-side stages are await-aware. When the wrapped callable is async,
`transforms_result` and `validates_result` apply to the awaited value, not
to the coroutine object:

```python
fetch = wrapture.binding(Service, "fetch")
fetch.on_call.transforms_result(lambda rows: rows[:10])
```

## Suspending and resuming

`suspend()` makes an applied wrapper completely inert without removing it;
`resume()` reactivates it:

```python
charge = wrapture.binding(Gateway, "charge").apply(suspended=True)
...
charge.resume()
...
charge.suspend()
charge.on_call.returns({"id": "other"})    # reconfigure with nothing in flight
charge.resume()
```

Unlike `remove()` followed by `apply()`, suspension changes nothing
structural: the wrapper keeps its position in the wrapper chain, so it is
safe to toggle while other parties have wrapped the same target. Calls that
arrive while suspended run the original callable and are counted on
`charge.suspended_calls`.

`remove()` clears suspension, so a re-applied binding starts active unless
`apply(suspended=True)` says otherwise.

## Groups

`bindings()` creates several bindings at once, named by keyword, that apply
and remove as a unit:

```python
with wrapture.bindings(charge=(Gateway, "charge"),
                       ledger=(Ledger, "record")) as group:
    group.charge.on_call.returns({"id": "stub"})
    group["ledger"].suspend()
```

If applying any member fails, the members already applied are removed
again, so a group never half-applies. `suspend()`, `resume()` and
`apply(suspended=True)` work across the whole group.

## Modes

A binding is either `callable` or `attribute` mode, detected from what is
found at the target: functions, lambdas, staticmethods and classmethods are
callable; properties, `__slots__` members and plain data are attributes.
The mode selects which behaviour namespaces exist: `on_call` for callable
bindings; `on_get`, `on_set` and `on_delete` for attribute bindings.
Accessing a namespace the mode does not support raises `WrongModeError`.

A callable object stored as a data attribute is ambiguous and is treated
as callable; pass `mode="attribute"` or `mode="callable"` to override the
detection:

```python
wrapture.binding(Model, "author", mode="attribute")
```

```{note}
Attribute mode is not implemented yet. Its API shape is present and every
operation raises `NotImplementedYetError`, including `apply()` on an
attribute-mode binding. When it is implemented, verify that a binding
created with an explicit `mode=` and a misspelled name still fails with
`AttributeError` at `apply()`, since such bindings skip the creation-time
check.
```

## Escape hatch to wrapt

The underlying wrapt handle and the patch coordinates are exposed, so
anything core wrapt can do remains reachable:

```python
charge.wrapper     # the wrapt FunctionWrapper handle, or None while unapplied
charge.target
charge.name

wrapt.unwrap_object(charge.target, charge.name, charge.wrapper)   # what remove() does
```
