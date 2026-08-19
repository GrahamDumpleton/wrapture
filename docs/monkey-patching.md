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

`target` is a module, class, instance, or a string. A string target uses
the colon convention that event paths, config observe targets and
`discover()` share: `"module"` or `"module:path"`. `name` is the path
from the target to the attribute, so these three are the same binding:

```python
wrapture.binding(myapp.gateway, "Gateway.charge")
wrapture.binding("myapp.gateway:Gateway", "charge")
wrapture.binding("myapp.gateway", "Gateway.charge")
```

When the member has an owning class, prefer the colon form: point
`target` at the owner and keep `name` the bare member name. That reads
consistently with `discover()` and config observe entries, where the
target is always the owner whose members are being selected.

Creating a binding does not patch anything. Declaring bindings at class or
module scope is safe; the patch is only installed when you ask for it.

A misspelled attribute name raises `AttributeError` at creation, on the
line that made the mistake. This check is a side effect of mode detection
(see [Binding modes](#binding-modes-call-versus-attribute)), so it has two exceptions. Passing
an explicit
`mode=` skips detection, and a typo in such a binding only surfaces when
`apply()` resolves the target. Passing `missing_ok=True` deliberately
accepts a name that does not resolve, to allow binding a name that
genuinely is not defined on the class, typically one assigned in
`__init__`; a typo is then accepted too.

The target module must already be imported. wrapt's trailing `?` syntax for
deferred patching is rejected with `DeferredTargetError`, because a binding
must hold the wrapper it applied in order to remove, suspend and report on
it. To patch a module the application has not imported yet, create the
binding inside a post-import hook instead; see
[patching a module before it is imported](#patching-a-module-before-it-is-imported).

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

## Call behaviour: changing what a call does

Behaviour for calls is configured through the `on_call` namespace. Every
method returns the binding, so configuration chains with `apply()`.

### Substituting results and failures

```python
charge.on_call.returns(value)      # return value; the real callable never runs
charge.on_call.raises(exc)         # raise exc; the real callable never runs
```

Both replace the call outright: the wrapped callable is never invoked,
matching what `unittest.mock` does with `return_value` and `side_effect`.
Everything else in the namespace runs the real callable and intervenes
around it.

### Transforming and validating

```python
charge.on_call.transforms_args(fn)      # fn(args, kwargs) -> (args, kwargs)
charge.on_call.transforms_result(fn)    # fn(result) -> result
charge.on_call.validates_args(check)    # check(*args, **kwargs); call unchanged
charge.on_call.validates_result(check)  # check(result); result unchanged
```

The real callable runs; each of these adjusts or inspects one side of
the call. A check reports failure the same way a test does: by raising,
and a plain `assert` inside the check is enough. Whatever the check
returns is ignored, so returning False fails nothing; there is no
wrapture-supplied validation exception.

```python
def positive_amount(amount, currency="USD"):
    assert amount > 0, f"amount must be positive, got {amount}"

charge.on_call.validates_args(positive_amount)
```

### Wrapping with a decorator

When touching one side at a time is not enough, `decorates()` controls
the whole call: it decides whether and how the real callable is invoked,
and what the caller gets back. For example, running the real call and
raising afterwards, to simulate a response lost after the operation
actually succeeded:

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

`decorates()` takes a plain function with wrapt's wrapper signature,
`fn(wrapped, instance, args, kwargs)`: the same function you would apply
`@wrapt.decorator` to, or hand to `FunctionWrapper` directly. A wrapper
written for a production decorator can therefore move to `decorates()`
unedited. Pass that undecorated function, not the result of applying
`@wrapt.decorator` to it, which is a decorator rather than a wrapper.

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

`passes_through()` drops both the terminal and every composing stage,
so the real call runs untouched, while leaving the patch installed:

```python
charge.on_call.passes_through()
```

(`reset()` does the same and also discards any later phases; see
[phased behaviour](#phased-behaviour-changing-what-a-call-does-over-time).)

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

## Phased behaviour: changing what a call does over time

Everything above configures one behaviour that holds until you change
it. Tests often need the behaviour to change on its own as the code
under test keeps calling: fail twice and then succeed, hand out a
sequence of canned responses, run the real thing until it breaks and
then fail fast. Rather than a list that mixes return values with
exceptions, wrapture models this as **phases**: the behaviour you
configure on `on_call` is phase 0, and `then()` adds the phase that
takes over from it, with the argument saying when the hand-over
happens.

```python
charge = wrapture.binding(Gateway, "charge")
charge.on_call.raises(TimeoutError("down"))     # phase 0: the gateway is down

recovered = charge.on_call.then(after=2)        # once phase 0 has handled two calls
recovered.passes_through()                      # the real call runs
```

The first two calls raise, every call after that is real. Each phase
is a complete behaviour of its own, with the same verbs as `on_call`,
and nothing is inherited between phases: a phase with no terminal runs
the real operation, a phase with no stages runs none. Stating
`passes_through()` on a fresh phase is therefore optional, and worth
writing when running the real thing is the point of the phase.

`then()` is relative to the namespace it is called on:
`charge.on_call.then(...)` is the phase after phase 0, and
`recovered.then(...)` is the phase after `recovered`. Calling `then()` again on the same namespace
returns the same successor rather than adding another, so setup code
that runs twice does not grow the chain. The last phase, having no
successor, stays active for good.

The verbs on a phase return the phase, so a phase can be configured in
one chain (`then(after=1).validates_args(check).returns(b)`); holding
it in a variable named for what the phase is, and configuring it line
by line as with `on_call`, usually reads better.

### When a phase ends

Three kinds of exit condition, one per `then()`:

- `then(after=n)`: after this phase has handled `n` more calls.
- `then(until=fn)`: once `fn(event)` is true for a call this phase
  handled. The event is the same `Event` a timeline records, seen as
  the caller saw it: `arguments` normalised, `result` after any
  `transforms_result`, `exception` set if anything in the pipeline
  raised. It is evaluated whether or not a timeline is running.
- `then()`: no condition of its own. The phase ends when the test
  calls `binding.advance()`, or, for a phase whose terminal is
  `returns_from()`, when its sequence runs out (below).

`advance()` works whatever the exit condition, so a test can force the
next phase early, and past the last phase it does nothing. Exhaustion
of a `returns_from()` sequence likewise ends its phase whatever the
condition, whichever comes first. `binding.phase` is the index of the
active phase, so a test can assert how far the chain got. Phases
restart at 0 on every `apply()`; `suspend()`/`resume()` leave them
alone.

A circuit breaker: run the real call until one fails, then fail fast:

```python
def failed(event):
    return event.exception is not None

fetch = wrapture.binding(Client, "fetch")
fetch.on_call.passes_through()

tripped = fetch.on_call.then(until=failed)
tripped.raises(CircuitOpen())
```

Stepping from the test, when the trigger is not in this binding's own
calls. Here the remote stays down until a health check, itself a
binding, reports it healthy:

```python
remote = wrapture.binding(Client, "request")
remote.on_call.raises(ConnectionError("down"))

online = remote.on_call.then()
online.passes_through()

health = wrapture.binding(Monitor, "check")

def note_recovery(status):
    if status == "healthy":
        remote.advance()

health.on_call.validates_result(note_recovery)
```

When the condition is visible in the call itself, `then(until=...)`
says it more directly than a stage calling `advance()`.

### Sequences: returns_from()

For "return the next value on each call", a phase per value would be
tiresome, so `returns_from(iterable)` is a terminal that draws
successive values, one per call, lazily; a generator or
`itertools.cycle()` works. When the sequence runs out the phase ends,
and the call that found it empty is handled by the successor. A bare
`then()` after a sequence therefore means "when it is exhausted"
rather than "on advance() only":

```python
lookup.on_call.returns_from(numbers)     # phase 0: one value per call

settled = lookup.on_call.then()          # once the sequence is exhausted
settled.returns(default)                 # ...and this value from then on
```

With no successor, running out raises `SequenceExhaustedError` at the
call site. `iter()` is called on the iterable afresh at each
`apply()`, so a list restarts and a generator continues.

A known sequence of "random" numbers makes code that samples or jitters
deterministic without seeding tricks:

```python
with wrapture.binding(random, "random").on_call.returns_from([0.1, 0.9, 0.5]):
    ...
```

`random.random` is a module attribute, so this catches
`random.random()` callers; code holding its own `random.Random()`
instance is covered by binding `random.Random` instead, and
`from random import random` at import time escapes, as with mock.

Stages compose around each drawn value as they do around any
terminal, so `validates_result()` on the same phase can refuse a
particular value when it comes through.

### mock's side_effect list, spelled out

`side_effect=[a, b, Err]` becomes one phase per regime, values and
exceptions kept apart:

```python
lookup.on_call.returns_from([a, b])

failing = lookup.on_call.then()
failing.raises(Err)
```

### Phases and the timeline

Events of a phased binding carry the index of the phase that handled
them as `event.phase` (None for a binding with a single phase), so a
recording can be filtered by regime with `in_phase(n)`:

```python
with wrapture.timeline(charge) as tape:
    service.place_order(...)

tape.for_binding(charge).in_phase(0).assert_times(2)
tape.for_binding(charge).in_phase(1).assert_once()
assert charge.phase == 1
```

`binding.phase` counts transitions, `in_phase()` counts calls; a phase
can be entered and left without handling a call, so the two answer
different questions.

### Attribute bindings and groups

`on_get`, `on_set` and `on_delete` have `then()` too, each operation
with its own chain, and `on_get` has `returns_from()`. Reads are easy
to trigger by accident (`repr`, `hasattr`, a debugger), so pair a read
sequence with a successor or `itertools.cycle()`. `binding.phase`
refers to the one operation that has phases; with phases on more than
one, ask `binding.on_get.phase` (or `on_set.phase`, ...) instead. A
binding group's `advance()` advances every member together.

`passes_through()` on a base namespace clears phase 0 only; to drop the
whole chain and start again, use `reset()`.

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

## Binding groups

`bindings()` creates several bindings at once, named by keyword, that apply
and remove as a unit. Like `binding()`, it only declares: the members are
reachable by attribute or item access for configuring behaviour, and
nothing is applied until the group is:

```python
group = wrapture.bindings(charge=(Gateway, "charge"),
                          ledger=(Ledger, "record"))
group.charge.on_call.returns({"id": "stub"})
group["ledger"].on_call.raises(TimeoutError("down"))

with group:
    ...
```

Configuring behaviour before applying means no call can slip through in
its real form between the patch landing and the behaviour being set.
Members can still be reconfigured while the group is applied, as with
any binding.

If applying any member fails, the members already applied are removed
again, so a group never half-applies. `suspend()`, `resume()` and
`apply(suspended=True)` work across the whole group.

## Binding modes: call versus attribute

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
terminal rules as `on_call`, each has its own `passes_through()`, and
validation checks fail the operation by raising, exactly as in
`on_call`. In
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

### Decorating a method on the fly

Because `on_get` runs with the instance in hand, an attribute binding on
a method can mint a wrapper for the bound method at each access, using
wrapt directly. That makes the decoration decision per access rather
than per definition:

```python
import wrapt

def audit(wrapped, instance, args, kwargs):
    record(instance, args)
    return wrapped(*args, **kwargs)

charge = wrapture.binding(Gateway, "charge", mode="attribute")

def selective(read, instance):
    bound = read()
    if instance.audited:
        return wrapt.FunctionWrapper(bound, audit)
    return bound

charge.on_get.decorates(selective)
```

Only instances flagged `audited` get the wrapper; everything else
receives the bare bound method. The `instance` argument inside `audit`
is correct without further work: `read()` returns a bound method, and
`wrapt.FunctionWrapper` recognises one, extracting `__self__` as the
instance when called. Method semantics survive the wrapper: equality
between accesses, signature introspection, `__name__` and `__self__`
all behave, and a bound method stored as a callback and called later
stays decorated.

Prefer a callable-mode binding for unconditional decoration: it installs
one wrapper once, where this pattern allocates a wrapper per access.
Note also that class-level access bypasses it: `Gateway.charge(obj, 1)`
reaches the raw function, since no instance access occurs.

Two limits worth knowing here. Binding an attribute of a module is
refused with `NotImplementedYetError`, because module attribute access
does not go through class descriptors. And the target must resolve to a
class: an instance target is refused with `TypeError`, since the
descriptor is installed on the class and would affect every instance,
not just the one given. See [Known limitations](known-limitations.md)
for the full list, including that attribute bindings intercept access
made through instances only.

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

## Patching a module before it is imported

A binding needs its target to exist, but application code often wants
to patch a third-party module it does not itself import first, and
without caring which of its own modules eventually does.
`wrapture.when_imported` handles the ordering: it is wrapt's
post-import hook decorator, re-exported so this one job does not
require importing wrapt alongside wrapture. The decorated function
runs with the module as its argument the moment that module is first
imported, or immediately if it already has been, so it is the place
to create and apply the binding:

```python
import wrapture

patches: list[wrapture.Binding] = []


@wrapture.when_imported("requests.sessions")
def _patch_requests(module):
    def with_tenant(args, kwargs):
        headers = {**(kwargs.get("headers") or {}), "X-Tenant": "acme"}
        return args, {**kwargs, "headers": headers}

    request = wrapture.binding(module.Session, "request")
    request.on_call.transforms_args(with_tenant)

    patches.append(request.apply())
```

Register the hook early, typically from the application package's
`__init__` or a startup module, and the patch is in place before the
first call regardless of who imports the library. Registering after
the module has already been imported is harmless: the hook simply
runs at once. Keep the applied binding somewhere, a module-level list
or a [binding group](#binding-groups), because the hook's local
variable is gone as soon as it returns and the handle is what
`suspend()`, `remove()` and `discover()` work from. The function form,
`wrapture.register_post_import_hook(callback, "module.name")`, does
the same without the decorator, for patches built in a loop or from
data. [Changing what a third-party library does](example-third-party-libraries.md#applying-before-the-library-is-imported)
walks through a hook firing on a real import, and the config file's
`[[setup]]` entry, described in
[configuring from a file](ad-hoc-tracing.md#configuring-from-a-file),
gives the same trigger without any code in the application.

## Escape hatch: dropping down to wrapt

The underlying wrapt handle and the patch coordinates are exposed, so
anything core wrapt can do remains reachable:

```python
charge.wrapper     # the wrapt FunctionWrapper handle, or None while unapplied
charge.target
charge.name

wrapt.unwrap_object(charge.target, charge.name, charge.wrapper)   # what remove() does
```
