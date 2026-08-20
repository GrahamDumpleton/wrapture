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

The positional arguments name a location. The first is a module, class,
instance, or a string using the colon convention that event paths,
config observe targets and `discover()` share: `"module"` or
`"module:path"`. Each further positional string is an attribute step
from there, itself possibly dotted, so these are all the same binding:

```python
wrapture.binding(myapp.gateway, "Gateway.charge")
wrapture.binding(myapp.gateway, "Gateway", "charge")
wrapture.binding("myapp.gateway:Gateway", "charge")
wrapture.binding("myapp.gateway", "Gateway.charge")
wrapture.binding("myapp.gateway:Gateway.charge")
```

In a string the colon is the boundary between the module and the
attribute path; module names contain dots too, so
`binding("myapp.gateway.Gateway.charge")` cannot be resolved and says
so. When the member has an owning class, prefer the colon form: point
the string at the owner and keep the last step the bare member name.
That reads consistently with `discover()` and config observe entries,
where the target is always the owner whose members are being selected.

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

In a test, a third form applies a binding around a whole test function:
`wrapture.bound()` takes the same addressing arguments and the same
chain, and is used as a decorator, with the binding injected into the
test as an argument. See
[scoping with decorators](unit-testing.md#scoping-with-decorators).

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
method returns the namespace itself, so several verbs chain on the
channel without naming it again, and the namespace stands in for its
binding everywhere one is expected: as a `with` target, passed to
`timeline()`, applied with `apply()`, or asked for its `events`.

```python
charge = wrapture.binding(Gateway, "charge")

with charge.on_call.transforms_args(fn).returns(None):
    ...
```

### Substituting results and failures

```python
charge.on_call.returns(value)      # return value; the real callable never runs
charge.on_call.raises(exc)         # raise exc; the real callable never runs
```

Both replace the call outright: the wrapped callable is never invoked,
matching what `unittest.mock` does with `return_value` and `side_effect`.
Everything else in the namespace runs the real callable and intervenes
around it.

A call the real callable never sees is still checked against its
signature. A binding is **strict** by default: whenever the active
behaviour has a terminal (`returns`, `raises`, `returns_from` or
`decorates`), the call is bound to the target's signature first, and
one that does not fit raises `TypeError` exactly as the real call
would, before anything is counted or recorded. That is the safety
`unittest.mock`'s `create_autospec` gives a mock, without asking for
it: a call site that drifts from the signature cannot pass on the
strength of a stub. Pipelines without a terminal run the real callable, which does
its own checking, so a `transforms_args` that deliberately adapts the
call shape is left alone; targets whose signature cannot be read
(some C-implemented callables) are not checked.
`binding(..., strict=False)` switches the check off, for the rare
patch that means to accept a call shape the target would not, say a
decorator that takes a test-only keyword.

```python
charge = wrapture.binding(Gateway, "charge")
charge.on_call.returns({"id": "stub"})

with charge:
    gateway.charge(500, bogus=True)   # TypeError: Gateway.charge (stubbed): got an unexpected keyword argument 'bogus'
```

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
to the coroutine object (the [testing async code](example-async-code.md)
example works through the testing side of all this):

```python
fetch = wrapture.binding(Service, "fetch")
fetch.on_call.transforms_result(lambda rows: rows[:10])
```

Stubs follow the target's calling convention too. On a coroutine
function, `returns()`, `returns_from()` and `raises()` deliver their
outcome on await: the call still hands back a coroutine, so code
written as `await service.fetch(1)` works against the stub exactly as
against the real method, and a stubbed exception is raised where the
real one would be, at the await. On an async generator function,
`returns(iterable)` hands back an async generator over the iterable
(an async iterable is passed through as it is) and `raises()` raises
on the first iteration. Argument stages and the strict signature check
still run at the call, where a coroutine function reports a bad call
shape too; `decorates()` owns its outcome and returns whatever it
returns. A stub has no outcome to inspect, so it goes by what
introspection reports (`inspect.iscoroutinefunction`), which wrapt's
`mark_as_async` and `mark_as_sync` can correct for a function whose
signature lies.

```python
fetch = wrapture.binding(Service, "fetch")         # async def fetch
fetch.on_call.raises(TimeoutError("down"))

async def test_retries_after_timeout():
    with fetch:
        with pytest.raises(TimeoutError):
            await service.fetch(1)                   # raised here, not at the call
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
`from random import random` at import time escapes, as it does from
a `unittest.mock` patch.

Stages compose around each drawn value as they do around any
terminal, so `validates_result()` on the same phase can refuse a
particular value when it comes through.

### unittest.mock's side_effect list, spelled out

`unittest.mock` accepts a list for `side_effect`, consumed one entry
per call, with exceptions raised and anything else returned. With
phases, `side_effect=[a, b, Err]` becomes one phase per regime, values
and exceptions kept apart:

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

`bindings()` groups bindings under names, to apply and remove as a
unit. Each member is an ordinary `binding()`, so it carries its own
mode and options; the keyword is the name it is reached by on the
group, by attribute or item access, and its label stays its own. Like
`binding()`, a group only declares: nothing is applied until the group
is:

```python
group = wrapture.bindings(charge=wrapture.binding(Gateway, "charge"),
                          ledger=wrapture.binding(Ledger, "record"))
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
again, so a group never half-applies; `remove()` runs in reverse order
of application. `suspend()`, `resume()`, `advance()` and
`apply(suspended=True)` work across the whole group. That is what a
group gives over a nested `with` of separate bindings: one lifecycle
with rollback, and one object to hand to a fixture or `timeline()`.

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

A third mode, `value`, is never detected: it is selected by naming a
slot with `attr=` or `item=` instead of what to wrap, and holds a
value in that slot rather than intercepting anything. See
[Value bindings](#value-bindings-holding-a-value-in-place).

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

### Module attributes

A module attribute is bound the same way, and a module's plain data is
detected as attribute mode, so a constant, a flag or a settings object
held on a module gets `on_get`, `on_set` and `on_delete` with the same
vocabulary, phases and events:

```python
>>> import types, wrapture
>>> config = types.ModuleType("config")
>>> config.TIMEOUT = 30

>>> timeout = wrapture.binding(config, "TIMEOUT")
>>> timeout.mode
'attribute'

>>> _ = timeout.on_get.returns(5)
>>> _ = timeout.on_get.then(after=2).passes_through()
>>> _ = timeout.on_set.raises(RuntimeError("frozen while the test runs"))

>>> with timeout, wrapture.timeline() as tape:
...     print(config.TIMEOUT, config.TIMEOUT, config.TIMEOUT)
...     try:
...         config.TIMEOUT = 1
...     except RuntimeError as exc:
...         print(exc)
5 5 30
frozen while the test runs

>>> [event.kind for event in tape.for_binding(timeout)]
['get', 'get', 'get', 'set']
>>> config.TIMEOUT
30

```

Under the covers the descriptor cannot go on `ModuleType` itself, so
the binding gives the module a private subclass of its type, assigned
to the module's `__class__` while any binding on that module is
applied, and puts the descriptor there. The value stays in the
module's `__dict__`, every binding on the module shares the one class,
and the original type is restored when the last binding is removed.
Two consequences are visible: `type(module) is types.ModuleType` is
False while a binding is applied (`isinstance` and `inspect.ismodule`
are unaffected, and the class is named `module` so reprs and messages
read the same), and a module whose type does not allow `__class__`
assignment, which some extension modules do not, is refused with
`TypeError` at `apply()`.

What is intercepted is access *through the module object*:
`config.TIMEOUT`, `getattr(config, "TIMEOUT")`, `setattr`, `del`. Code
that did `from config import TIMEOUT` holds the value already, and
reads through `vars(config)` or `config.__dict__` bypass the
descriptor, as they do for an attribute binding on a class. When all
that is wanted is a different value in place for the duration of a
test, with nothing observed,
`binding(config, attr="TIMEOUT").overrides(5)` is the
[value binding](#value-bindings-holding-a-value-in-place) form, and a
value binding applied on top of an interception binding on the same
name is seen by it as a set, since that is what it does.

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

One limit worth knowing here: the target must resolve to a class or a
module. An instance target is refused with `TypeError`, since the
descriptor is installed on the class and would affect every instance,
not just the one given. See [Known limitations](known-limitations.md)
for the full list, including that attribute bindings intercept access
made through instances only.

## Value bindings: holding a value in place

Everything above wraps something: a call or an attribute access passes
through wrapture, which records it or changes what it does. Some
patches are not like that. A test wants an environment variable set,
a key in a settings dict changed, a module constant lowered, an
attribute on one object pointed elsewhere, for the duration of the
test and then put back, and nothing needs to be observed. That is a
**value binding**: name the owner positionally, name the slot in it
with `attr=` or `item=`, and say what the slot should hold:

```python
timeout = wrapture.binding("myapp.config", attr="TIMEOUT").overrides(0.5)
api_key = wrapture.binding(os.environ, item="API_KEY").overrides("sk_test")
```

`attr=` names an attribute of the owner; `item=` names a mapping
entry, any key: an `os.environ` variable, a dict key, a `sys.modules`
name, a list index. The positional part names the owner, in every
form [Creating a binding](#creating-a-binding) allows, so
`binding("os", "environ", item="API_KEY")` and
`binding("myapp.config", "SETTINGS", item="currency")` read the way
the code does. Three verbs, each returning the binding so it chains
into `apply()` or a `with`:

- `overrides(value)`: the slot holds `value` while applied.
- `hides()`: the slot is **absent** while applied: the variable is
  unset, the key is not in the mapping, the attribute is not on the
  owner. This is what "the setting is missing" tests need, which
  `overrides(None)` cannot say (None is a value that is there).
- `passes_through()`: leave the slot as it really is. A binding starts
  in this state, and it is the way back to it on an applied binding
  without removing it.

```python
with wrapture.binding(os.environ, item="API_KEY").hides():
    with pytest.raises(ConfigError, match="API_KEY"):
        make_client()
```

`apply()` (or entering the `with`) notes what the slot held, or that
it held nothing, and writes the configured state; `remove()` puts the
prior state back, deleting the slot if it did not exist before. The
owner is never replaced, so every reference to it sees the change:
`os.environ` is one object, and a settings dict imported elsewhere
with `from config import SETTINGS` is the same dict the binding wrote
into. Rebinding the *attribute* (`binding("config", attr="SETTINGS").overrides({...})`)
is different: it gives `config.SETTINGS` a new
object, which reaches code that reads it through the module and not
code that copied the name at import; use `item=` on the dict itself
when the point is a changed entry, or a
[mapping binding](#mapping-bindings-substituting-a-mappings-content) when the point is
the whole content.

The verbs may be called before or after `apply()`, and on an applied
binding they take effect at once, so the fixture shape is one binding
applied for the test and a verb per case:

```python
@pytest.fixture
def api_key():
    with wrapture.binding(os.environ, item="API_KEY") as key:
        yield key                     # applied, holding nothing yet

def test_sandbox_key(api_key):
    api_key.overrides("sk_test")
    ...

def test_missing_key(api_key):
    api_key.hides()
    ...
```

Everything around bindings applies. `suspend()` puts the prior state
back until `resume()`; `active` reports whether the slot still holds
what the binding put there, so a teardown can see that something else
overwrote it (`repr()` shows `displaced`); a
[group](#binding-groups) applies and removes several together, in
order, with rollback; the pytest plugin's leak sweep reports a value
binding left applied like any other.

What a value binding does not do is record: there is no call to
observe, only a value in a slot. It has no `on_*` namespaces, no
events, no phases, and the recording options (`capture=`, `stack=`,
`when=`) are refused rather than ignored. If what you want is to see
who reads a setting, or to transform the value as it is read, give the
code an accessor (`get_setting("TIMEOUT")`, `config.timeout()`) and
bind that in callable mode: reads then go through one callable with
the whole vocabulary, and it is usually better code anyway.

Some cases worth knowing:

- An instance attribute, on one object only:
  `binding(client, attr="base_url").overrides("http://localhost:9999")`.
  If the value
  came from the class, the instance is left without its own copy
  afterwards, exactly as before. (Positional
  `binding(client, "base_url")` is still refused: an attribute binding
  installs a descriptor on a class, and the error now names `attr=`
  for this case.)
- A module constant:
  `binding("myapp.http", attr="TIMEOUT").overrides(0.01)`. Positional
  `binding("myapp.http", "TIMEOUT")` is the
  [interception form](#attribute-bindings), which sees and can shape
  each read; the value binding just holds the value.
- A stand-in module: `binding(sys.modules, item="boto3").overrides(fake)`,
  affecting imports made while applied, as
  `unittest.mock.patch.dict(sys.modules, ...)` does.
- Replacing a function wholesale rather than wrapping it,
  `binding("myapp.clock", attr="now").overrides(lambda: FIXED)`, when
  a whole different object is wanted; `on_call.returns()` or
  `decorates()` is usually the better tool, since the binding then
  records and can be phased.
- `attr=` takes no `mode=`: to wrap what an attribute holds, name it
  positionally, that spelling already exists. `item=` also takes
  `mode="callable"`, `"wsgi"` or `"asgi"`, since nothing else can
  reach a callable or an application held in a mapping: the entry is
  wrapped in place with the whole vocabulary of that mode and the
  original put back on `remove()`. A handler in a dispatch table,
  with phases and recording:

  ```python
  handler = wrapture.binding("myapp.routes", "HANDLERS", item="GET", mode="callable")
  handler.on_call.raises(ServiceUnavailable())
  handler.on_call.then(after=2).passes_through()

  with handler, wrapture.timeline() as tape:
      ...
  ```

  This reaches what `observed()` was previously the only tool for,
  when the callable does sit in a mapping; the usual caveat applies,
  as for a module function, that whoever pulled `HANDLERS["GET"]`
  into a local before `apply()` holds the original.

## Mapping bindings: substituting a mapping's content

A value binding changes one slot. Sometimes the thing to change is the
whole content of one mapping: the settings dict should hold exactly
these entries for the test, or the environment should be empty, and
every holder of that dict, including code that did `from config import
SETTINGS` at import time, should see it. That is `mode="mapping"` on a
location whose object is a mutable mapping; the object is mutated in
place and never replaced (the
[pinning configuration](example-pinning-configuration.md) example puts
this and the other configuration shapes to work together):

```python
>>> import types, wrapture
>>> config = types.ModuleType("config")
>>> config.SETTINGS = {"currency": "USD", "tax_rate": 0.2}
>>> SETTINGS = config.SETTINGS            # a holder, as `from config import SETTINGS`

>>> settings = wrapture.binding(config, "SETTINGS", mode="mapping")
>>> settings.mode
'mapping'

>>> with settings.overrides({"currency": "EUR"}):      # exactly these entries
...     print(SETTINGS, config.SETTINGS is SETTINGS)
{'currency': 'EUR'} True

>>> with settings.updates({"currency": "EUR"}):        # merged over what is there
...     print(SETTINGS)
{'currency': 'EUR', 'tax_rate': 0.2}

>>> SETTINGS                                          # put back, in order
{'currency': 'USD', 'tax_rate': 0.2}

```

Three verbs: `overrides(values)` makes `values` the whole content
while applied, and `overrides({})` is the empty mapping (there is no
`hides()` here, since a mapping's absent content is just no entries);
`updates(values)` merges the named keys over the original content;
`passes_through()` leaves the content as it is. For readers coming
from `unittest.mock`, `overrides()` is `patch.dict(d, values,
clear=True)` and `updates()` is `patch.dict(d, values)`; see
[coming from mock](coming-from-mock.md). As with a value binding the
verbs may be called before or after `apply()`, take effect at once on
an applied binding, and `remove()` restores exactly the entries that
were there at `apply()`, in their order. `values` is copied when the
verb is called, so changing the dict you passed afterwards changes
nothing. The entries' values are the same objects, not copies, so a
nested list or dict mutated during the test stays mutated (as it does
with `patch.dict`).

The target can be the mapping itself, `binding(os.environ,
mode="mapping")`, or any location form that reaches it, and `item=`
takes the mode too for a mapping held in an entry:
`binding(config, "DATABASE", item="primary", mode="mapping")`.
`attr=` does not: the object held in an attribute is what the
positional form names. A clean environment for a test is:

```python
with wrapture.binding(os, "environ", mode="mapping").overrides({"HOME": "/tmp/h"}):
    ...
```

Which of the three dict spellings to reach for:

| want | spelling |
|---|---|
| one entry changed or absent | `binding(d, item="k").overrides(v)` / `.hides()` |
| `config.SETTINGS` to be a different object (holders of the old one unaffected) | `binding("config", attr="SETTINGS").overrides({...})` |
| the one dict to hold these entries, for every holder | `binding(d, mode="mapping").overrides({...})` / `.updates({...})` |

Everything else is as for value bindings: `suspend()` puts the
content back until `resume()`, `active` reports whether the content
still matches what the binding wrote (for `updates()`, whether its own
keys still hold their values), groups apply and unwind in order, so a
mapping binding and an `item=` binding on the same dict stack, nothing
is recorded, and the recording options are refused. Two mapping
bindings on one dict unwind correctly when removed in reverse order;
removed out of order, each restores what it saw at its own `apply()`.

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

As on a binding's namespaces, every verb hands the namespace back, so
several verbs chain without naming the namespace again, and the
namespace stands in for its factory, including being callable with an
iterator:

```python
doubles = wrapture.iterator().on_item.transforms_item(lambda item: 2 * item)
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
