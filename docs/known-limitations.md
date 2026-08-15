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

## How a call was reached is not observable

Neither binding mode can distinguish a call made via the class
(`Gateway.charge(obj, 1)`) from one made via an instance
(`obj.charge(1)`). A callable-mode wrapper receives the same `instance`
either way, because wrapt deliberately normalises the two forms. An
attribute binding does not see class-level access at all, per the first
limitation. Distinguishing the access route requires a purpose-built
descriptor owning the attribute, which is outside what a binding does.

## Targets must already be imported

A binding always holds the wrapper it applied, so wrapt's deferred
patching (a trailing `?` on a string target, registering a post-import
hook that returns no handle) is rejected with `DeferredTargetError`.
Import the module first and bind against it.

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
