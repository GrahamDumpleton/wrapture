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

See [Attribute bindings](#attribute-bindings) for what the attribute-mode
namespaces do. A binding created with an explicit `mode=` and a
misspelled name fails with `AttributeError` at `apply()`, since such
bindings skip the creation-time check.

## Attribute bindings

An attribute-mode binding intercepts reads, writes and deletes of an
attribute by installing a data descriptor on the class, wrapping whatever
previously occupied the attribute: a plain class default, a property or
other descriptor, a `__slots__` member, or nothing at all with
`missing_ok=True`. The prior definition keeps working beneath the
interception: a property's getter, setter and deleter still run, writes
land in the instance dictionary when there is no prior setter, and reads
follow the normal lookup precedence, with an instance value beating a
plain class default.

Behaviour is configured through three namespaces, one per operation:

```python
status = wrapture.binding(Model, "status")

status.on_get.returns(value)        # reading gives value; no real read
status.on_get.transforms(fn)        # fn(value) -> value
status.on_get.validates(check)      # check(value); read passes unchanged
status.on_get.decorates(fn)         # fn(read, instance) -> value
status.on_get.raises(exc)           # raise exc instead of reading

status.on_set.transforms(fn)        # fn(value) -> value actually written
status.on_set.validates(check)      # check(value); write passes unchanged
status.on_set.decorates(fn)         # fn(write, instance, value)
status.on_set.rejects()             # AttributeError instead of writing

status.on_delete.validates(check)   # check(instance); delete passes
status.on_delete.decorates(fn)      # fn(erase, instance)
status.on_delete.rejects()          # AttributeError instead of deleting
```

Each namespace is an independent pipeline with the same composing and
terminal rules as `on_call`, and each has its own `passes_through()`. In
the `decorates()` forms, `read()`, `write(value)` and `erase()` perform
the real operation, so the function decides whether and how it happens.

```python
status = wrapture.binding(Model, "status")
status.on_set.validates(lambda value: check_transition(value))
status.on_delete.rejects()

with status:
    ...
```

The lifecycle is identical to callable bindings: apply and remove or a
context manager, groups (which can mix both modes), and honest
active/displaced state. A suspended attribute binding passes reads,
writes and deletes straight through, counting them on `suspended_calls`.
Two attribute bindings on the same name compose, and removal restores
the original definition exactly, including removing the shadowing slot
when the binding was over an inherited default or a `missing_ok` name.

With `missing_ok=True` the binding covers an attribute that exists only
on instances, typically assigned in `__init__`; reads raise
`AttributeError` until a value is written, exactly as without the
binding, and writes made in `__init__` pass through the binding's set
behaviour.

An attribute binding and a callable binding can be stacked on the same
method, in either order, to observe both the access and the call: the
attribute binding sees the lookup that produces the bound method, the
callable binding sees the call itself. The attribute binding needs an
explicit `mode="attribute"`, since detection classifies a method as
callable:

```python
access = wrapture.binding(Service, "ping", mode="attribute")
access.on_get.validates(record_access)

calls = wrapture.binding(Service, "ping")
calls.on_call.validates_args(record_call)
```

Two limits. Binding an attribute of a module is refused with
`NotImplementedYetError`, because module attribute access does not go
through class descriptors. And the target must resolve to a class: an
instance target is refused with `TypeError`, since the descriptor is
installed on the class and would affect every instance, not just the
one given.

## Iterators and generators

Behaviour on a binding runs when the target is called. A callable that
returns a generator or iterator produces its values later, one item at a
time, as the caller iterates, so `transforms_result` on such a target
transforms the iterator object itself, not the items it will produce.
The same applies on the way in: an argument may be a generator whose
items only flow once the callee iterates it. wrapture never wraps items
automatically; working per item is opted into explicitly.

The opt-in is an iterator proxy factory, created with `iterator()` and
configured through its `on_item` namespace:

```python
doubles = wrapture.iterator()
doubles.on_item.transforms_item(lambda item: 2 * item)
```

Unlike a binding, the factory has no target. It is applied by calling
it: each call takes one iterator and returns a new wrapped iterator that
applies the configured behaviour to every item passing through. One
factory can wrap any number of iterators.

Because the factory is a callable taking the iterator and returning the
wrapped iterator, it slots directly into a binding's pipeline on either
side of the call:

```python
# items produced by the result
rows = wrapture.binding(Repo, "rows")
rows.on_call.transforms_result(doubles)

# items consumed from an argument
consume = wrapture.binding(Sink, "consume")
consume.on_call.transforms_args(
    lambda args, kwargs: ((doubles(args[0]), *args[1:]), kwargs)
)
```

Use `decorates()` when wrapping is conditional or applies to both sides:

```python
def per_item(wrapped, instance, args, kwargs):
    result = wrapped(*args, **kwargs)
    if inspect.isgenerator(result):
        result = doubles(result)
    return result
```

`on_item` mirrors the composing half of `on_call`:

```python
doubles.on_item.transforms_item(fn)     # fn(item) -> item
doubles.on_item.validates_item(check)   # check(item); item passes unchanged
doubles.on_item.passes_through()        # drop all configured item behaviour
```

Three further namespaces cover how an iteration ends:

```python
doubles.on_finish.validates(check)      # normal exhaustion; check(value)
doubles.on_error.notifies(fn)           # iteration failed; fn(exc)
doubles.on_abandon.notifies(fn)         # closed before exhaustion; fn()
```

Finish checks receive the wrapped generator's return value, or None for
iterator kinds that have none, and completion stands unless the check
raises. Error hooks see the exception about to reach the consumer,
whether it came from the iterator's body, from an unhandled `throw()`,
or from an item stage. Abandon hooks fire when a started, unexhausted
generator is closed, explicitly or by garbage collection; a wrapper
closed before its first item is silent, and plain iterators have no
close protocol so never report abandonment. Each namespace has its own
`passes_through()`.

Behaviour is snapshotted each time the factory is applied: a wrapped
iterator applies the behaviour configured at the moment it was wrapped,
and reconfiguring the factory affects only iterators wrapped afterwards.
With no behaviour configured, applying the factory returns the iterator
unwrapped.

An iterator factory has no `suspend()` or `resume()`: suspension belongs
to bindings (see above), because only an applied binding has a live
presence on a target, while a factory acts only at the moment it is
applied. Since a factory is normally applied from a binding's behaviour,
suspending that binding is what stops further iterators being wrapped.
Note that either way, iterators already wrapped keep the behaviour
snapshotted when they were wrapped, for as long as they are iterated.

Wrapped generators keep their full protocol: `send()` values and
`throw()` are forwarded to the wrapped generator, `close()` closes it,
and its return value is preserved. Async generators and plain sync and
async iterators are supported by the same factory, each keeping its own
protocol.

Applying the factory to an iterable that is not an iterator, such as a
list, raises `TypeError` rather than silently replacing the container
with an iterator of a different type; call `iter()` on it first if that
is what you want.

## Escape hatch to wrapt

The underlying wrapt handle and the patch coordinates are exposed, so
anything core wrapt can do remains reachable:

```python
charge.wrapper     # the wrapt FunctionWrapper handle, or None while unapplied
charge.target
charge.name

wrapt.unwrap_object(charge.target, charge.name, charge.wrapper)   # what remove() does
```
