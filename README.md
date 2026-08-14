# wrapture

**Trace assertions without instrumenting your code.**

wrapture (`wrapt` + `capture`) is a Python library for attaching bindings to
arbitrary call sites, without modifying the code being observed, and doing
something useful with what flows through them.

It is a sibling project to [wrapt](https://github.com/GrahamDumpleton/wrapt)
and [autowrapt](https://github.com/GrahamDumpleton/autowrapt), building on the
safe monkey-patching machinery wrapt provides.

> **Status: early development.** The design is settled and a prototype exists,
> but the library is not yet ready for use. Nothing is published to PyPI.

## What it does

One mechanism, four uses, in increasing order of machinery:

1. **Monkey patching.** A clean lifecycle and behaviour vocabulary over
   wrapt's `wrap_object()`. Point at a method by name and stub it, fail it,
   transform its arguments or result, or wrap it with a decorator, then
   remove it again, with honest reporting if something else displaced the
   patch in the meantime. Useful entirely on its own, with nothing else
   switched on.

2. **Testing.** Observe and assert on how calls actually flowed through a
   *real* call graph (nesting, ordering, arguments and return values) and
   optionally intervene (stub, transform, fail-inject). Unlike a `Mock`,
   which fabricates values and cannot see calls an object makes to itself,
   wrapture watches the real code run. This makes it possible to test code
   with no injectable seams at all, and to assert on what *didn't* happen on
   an error path: inject a gateway timeout, then verify the ledger was not
   written, the receipt was not sent, and the compensating refund was issued.

3. **Ad-hoc tracing.** Attach bindings to a running application, including
   one you cannot modify or redeploy, and emit a structured, nested trace to
   process or chart elsewhere. Name a handful of methods and a call tree
   appears; no code changes required.

4. **Targeted profiling.** Use a binding as a *scope* within which CPython's
   own profiling machinery is active, so you can profile one subsystem of a
   live process instead of everything.

The distinction that matters: OpenTelemetry-style tracing requires the code
to already emit spans. `cProfile` requires whole-process activation. wrapture
requires neither: you point at a method by name and a trace appears.

## Why

No single existing tool covers "point at arbitrary methods, get a structured
nested trace, assert on it or export it, in tests or in production":

- `unittest.mock` records a flat call list, with no nesting and no return
  values, and a patched call returns a fabricated `MagicMock` rather than
  running the real code.
- Span-assertion tools (`logfire.testing`, OpenTelemetry's
  `InMemorySpanExporter`) require the code to already be instrumented.
- `sys.settrace` tools (`hunter`, `snoop`) give a firehose with no assertion
  API.
- `cProfile` cannot scope to a subsystem in a live process, and APM agents
  are all-or-nothing products rather than a toolkit.

wrapture fills that gap: a targeted call tree with normalized arguments and
return values, produced by naming the methods you care about, usable as a
testing assertion library, a tracing tool, or both at once.

## What it is not

- **Not a replacement for `unittest.mock`.** It complements mocking where
  code has seams; it exists for the code that doesn't.
- **Not a sampling profiler.** `py-spy` and `austin` do that better and
  without distortion.
- **Not a production APM.** It is a toolkit that APM-like things could be
  built on.
- **Not an OpenTelemetry competitor.** It should emit to OTel, not replace
  it.

## Requirements

- Python 3.12+
- [wrapt](https://github.com/GrahamDumpleton/wrapt) 2.4.0+

## License

BSD 2-Clause. See [LICENSE](LICENSE).
