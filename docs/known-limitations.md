# Known limitations

Known limits of what wrapture can intercept, with the reason for each
and a workaround where one exists. These are boundaries of the
mechanisms wrapture builds on, documented so a silent gap does not have
to be discovered the hard way.

## Attribute bindings intercept instance access only

An attribute binding observes what *instances* do with an attribute,
wherever the attribute is defined. An attribute defined on the class is
the normal case, and reads, writes and deletes made through an instance
are all intercepted. Access performed on the class object itself is not:

- A class-level read (`Model.status`) returns the installed descriptor
  before behaviour is consulted. The descriptor is a transparent proxy
  of the prior definition, so introspection and comparisons still
  behave, but `on_get` does not run.
- A class-level write (`Model.status = "x"`) cannot be intercepted, and
  replaces the descriptor outright, displacing the binding. The binding
  reports this honestly: `active` becomes False and `repr()` shows
  `displaced`.
- A class-level delete removes the descriptor the same way.

The reason is the descriptor protocol itself: descriptors on a class
fire for attribute access on its instances. The class is an instance of
its *metaclass*, so intercepting class-level access would require the
descriptor to live on the metaclass. For a class with a custom
metaclass, binding the attribute name on the metaclass does work as a
recipe, but almost every class's metaclass is `type`, which cannot be
patched.

## Module attributes cannot be bound in attribute mode

Binding an attribute of a module is refused with
`NotImplementedYetError`. A module is an instance of `ModuleType`, so
intercepting `module.attr` access needs a descriptor on the module's
type, which plain descriptor installation cannot provide. (CPython
allows assigning a `ModuleType` subclass to a module's `__class__`,
which is the known route should this ever be supported.)

Module-level *functions* are unaffected: callable-mode bindings on
module functions work normally.

## Attribute bindings install on the class, never one instance

An attribute binding with an instance as target is refused with
`TypeError`. The descriptor must be installed on the class, so it would
affect every instance, which is unlikely to be what a binding on one
instance was meant to do. Callable-mode bindings on a single instance
are supported and affect only that instance.

## Dynamically served attributes have no place to patch

An attribute produced by a module-level or class `__getattr__` exists in
no `__dict__`, so there is no owning location to install a wrapper on.
Resolution fails with wrapt's `PathResolutionError` naming the problem.

## Class access and instance access look the same

Neither binding mode can distinguish a call made via the class
(`Gateway.charge(obj, 1)`) from one made via an instance
(`obj.charge(1)`). A callable-mode wrapper receives the same `instance`
either way, because wrapt deliberately normalises the two forms. An
attribute binding does not see class-level access at all, per the first
limitation. Distinguishing the access route requires a purpose-built
descriptor owning the attribute, which is outside what a binding does.

A related question does have an answer: while the form the caller wrote
is not observable, the line of code that made the call is, with
`stack=` on the binding, per the stack capture section of the unit
testing page.

## Targets must already be imported

A binding always holds the wrapper it applied, so wrapt's deferred
patching (a trailing `?` on a string target, registering a post-import
hook that returns no handle) is rejected with `DeferredTargetError`.
Import the module first and bind against it, or create the binding
inside a `wrapture.when_imported` hook for the module, which runs with
the module as soon as it is imported (see
[patching a module before it is imported](monkey-patching.md#patching-a-module-before-it-is-imported)).

The config layer's `[[observe]]` and `[[setup]]` entries are the same
idea in file form: applying a config registers a post-import hook per
target and constructs the bindings when the module arrives, so
zero-code configuration does not carry this restriction. Deferral is a
property of when the hook runs, never a state a binding models.

## Calls on other threads may not be recorded

This limitation is about scoped recording, timelines and their tapes,
not about recording itself: a process sink registered with
`add_sink()` is visible to every thread and hears thread work with
nesting intact, see [ad-hoc tracing](ad-hoc-tracing.md). What follows
applies to
recording onto a timeline's tape.

The scoped recording state (which tapes are listening, what call is in
progress) lives in context variables, which is what makes concurrent
asyncio tasks record correctly isolated trees. Threads are the other
side of that coin: a thread that does not carry the caller's context
sees no scoped sinks, so its calls run normally, with behaviour still
applied, but record nothing onto the timeline's tape.

Whether a plain `threading.Thread` carries context depends on the
Python build. From Python 3.14, `Thread` accepts a `context=` argument
and inherits a copy of the caller's context by default where
`sys.flags.thread_inherit_context` is set, which is the free-threaded
default; on GIL builds, and everywhere on 3.12 and 3.13, threads start
with an empty context and do not record.

What wrapture guarantees is that the gap is loud rather than silent:
an observed operation that runs with no context while a timeline is
active elsewhere raises `RecordingGapWarning` (once per binding per
apply) and is counted on `Binding.missed_calls`, so a shorter tape than
expected can be explained. To record thread work deliberately, wrap the
thread's target with `propagate()`, called inside the timeline:

```python
thread = threading.Thread(target=wrapture.propagate(work))
```

Underneath this is just `contextvars.copy_context()`: each invocation
of the propagated callable runs in its own copy of the context that
was current when `propagate()` was called, so one propagated callable
can be shared by several threads, and on Python 3.14+ passing
`context=` to `Thread` directly achieves the same thing. The tape is
safe to record onto from several threads at once.

A propagated thread that outlives the timeline is safe by
construction: the tape closes when the scope exits, and events
arriving after that are discarded and counted on `Tape.discarded`
rather than appended, so a result already asserted on cannot change
shape. The count is visible in the tape's repr:
`<Tape: 7 events, 2 discarded after close>`.

Asyncio tasks are unaffected: every task runs in a copy of the context
it was created under. Copying a context copies variable bindings, not
the objects they refer to, so every task's binding points at the one
shared tape and their events all land there, visible to the parent's
assertions. Only the in-progress nesting state is per-task, which is
what keeps concurrent tasks' call trees from tangling. The asyncio thread bridges
split along the same context line as plain threads.
`asyncio.to_thread()` copies the caller's context per call and records
normally. `loop.run_in_executor()` propagates nothing per call: a pool
worker keeps whatever context existed when that worker thread was first
created, which on inheriting builds makes recording depend on pool
warm-up timing, and elsewhere means no context at all. Treat executor
work as unrecorded, and expect the gap warning for it.

## Iteration recording covers generators only

When a recorded call returns a generator or async generator, the event
tracks the iteration: item count, wall and body durations, the return
value at exhaustion, and a visibly unfinished event on abandonment, as
described on the unit testing page. That treatment keys on the returned
object actually being a generator. A call that returns any other kind
of iterator (`map()` and `filter()` objects, `itertools` results, a
custom class implementing `__next__`, or an already materialised list)
records that object as an ordinary by-reference result, with no item
count and no iteration lifecycle.

This is deliberate: only generators have the suspended-body semantics
the one-event-per-iteration model is built on, and wrapping arbitrary
iterators would substitute objects flowing through the program far more
broadly. For item-level visibility on other iterators, wrap them
explicitly with `iterator()` or record what matters with `annotate()`.

## Builtin and extension types cannot be patched

Attributes of types implemented in C (`list`, `dict`, `str`, and
extension types generally) cannot be replaced, so bindings on them fail
at `apply()` with the `TypeError` CPython raises for the assignment.
This is a CPython restriction, not a wrapture one. Wrap the Python-level
call sites that use such types instead.
