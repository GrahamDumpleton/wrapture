# wrapture

**Trace assertions without instrumenting your code.**

wrapture (`wrapt` + `capture`) is a Python library for attaching bindings to
arbitrary call sites, without modifying the code being observed, and doing
something useful with what flows through them.

It is a sibling project to [wrapt](https://github.com/GrahamDumpleton/wrapt)
and [autowrapt](https://github.com/GrahamDumpleton/autowrapt), building on
the safe monkey-patching machinery wrapt provides.

```{note}
wrapture is in early development. The design is settled and the monkey
patching layer is implemented, but the library is not yet released.
```

## What it does

One mechanism, four uses, in increasing order of machinery:

1. **Monkey patching.** A clean lifecycle and behaviour vocabulary over
   wrapt's `wrap_object()`. Point at a method by name and stub it, fail it,
   transform its arguments or result, or wrap it with a decorator, then
   remove it again, with honest reporting if something else displaced the
   patch in the meantime. Useful entirely on its own, with nothing else
   switched on.

2. **Unit testing.** Observe and assert on how calls actually flowed through a
   *real* call graph (nesting, ordering, arguments and return values) and
   optionally intervene (stub, transform, fail-inject). Unlike a `Mock`,
   which fabricates values and cannot see calls an object makes to itself,
   wrapture watches the real code run.

3. **Ad-hoc tracing.** Attach bindings to a running application, including
   one you cannot modify or redeploy, and emit a structured, nested trace to
   process or chart elsewhere. Name a handful of methods and a call tree
   appears; no code changes required.

4. **Targeted profiling.** Use a binding as a *scope* within which CPython's
   own profiling machinery is active, so you can profile one subsystem of a
   live process instead of everything.

The distinction that matters: most tracing and profiling tools either need
the code to have been written with them in mind, or can only be switched on
for the whole program at once. wrapture needs neither: you point at a
method by name and a trace appears.

## Documentation

```{toctree}
:maxdepth: 2

design-philosophy
monkey-patching
unit-testing
known-limitations
release-notes
```
