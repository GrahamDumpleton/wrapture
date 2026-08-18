# wrapture

**Trace assertions without instrumenting your code.**

wrapture (`wrapt` + `capture`) is a Python library for attaching bindings to
arbitrary call sites, without modifying the code being observed, and doing
something useful with what flows through them.

It is a sibling project to [wrapt](https://github.com/GrahamDumpleton/wrapt)
and [autowrapt](https://github.com/GrahamDumpleton/autowrapt), building on
the safe monkey-patching machinery wrapt provides.

```{note}
wrapture is in early development. The monkey patching, unit testing
and ad-hoc tracing layers are implemented, including WSGI and ASGI
request tracing. Development previews are published to
[PyPI](https://pypi.org/project/wrapture/); the API may still shift
before 1.0.0.
```

## What it does

One mechanism, three uses, in increasing order of machinery:

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
   appears; no code changes required: with a `wrapture.toml` naming the
   methods and a sink, `python -m wrapture manage.py runserver` traces the
   application untouched. With [autowrapt](https://github.com/GrahamDumpleton/autowrapt)
   installed, not even the launcher is needed: `AUTOWRAPT_BOOTSTRAP=wrapture`
   in the environment applies the same config at interpreter startup, so the
   program starts with plain `python`.

The distinction that matters: most tracing tools either need the code to
have been written with them in mind, or can only be switched on for the
whole program at once. wrapture needs neither: you point at a method by
name and a trace appears.

## Thirty seconds of it

None of the classes below import wrapture or know they are observed:

```python
place = wrapture.binding(OrderService, "place")
charge = wrapture.binding(Gateway, "charge")
record = wrapture.binding(Ledger, "record")

with wrapture.timeline(place, charge, record) as tape:
    OrderService().place(500)

print(tape.tree())
```

```
OrderService.place(amount=500)  -> {'id': 'ch_500', 'amount': 500}
  Gateway.charge(amount=500, currency='USD')  -> {'id': 'ch_500', 'amount': 500}
  Ledger.record(entry={'id': 'ch_500', 'amount': 500})  -> 'led_ch_500'
```

New here? Start with the getting started page; everything on it can be
pasted into an interpreter. Coming from `unittest.mock`? The comparison
page maps each mock idiom to its wrapture counterpart.

## Documentation

The guides are organised by mechanism: bindings and behaviours, then
recording in tests, then tracing a running process. The worked examples
are organised by the question you arrive with, and each one combines
several of those mechanisms on a small concrete scenario, building up
from the problem to a finished test or configuration.

```{toctree}
:maxdepth: 2
:caption: Start here

getting-started
coming-from-mock
design-philosophy
```

```{toctree}
:maxdepth: 2
:caption: Guides

monkey-patching
unit-testing
ad-hoc-tracing
wsgi-tracing
asgi-tracing
scheduled-tracing
```

```{toctree}
:maxdepth: 2
:caption: Worked examples

example-external-services
example-resource-hygiene
example-streaming-data
example-request-timing
example-service-over-time
example-third-party-libraries
```

```{toctree}
:maxdepth: 2
:caption: Reference

known-limitations
release-notes
```
