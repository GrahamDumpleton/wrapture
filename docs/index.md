# wrapture

**Wrap anything, capture everything, change nothing.**

wrapture (`wrapt` + `capture`) is a Python library for attaching bindings to
arbitrary call sites, without modifying the code being observed, and doing
something useful with what flows through them.

It is a sibling project to [wrapt](https://github.com/GrahamDumpleton/wrapt)
and [autowrapt](https://github.com/GrahamDumpleton/autowrapt), building on
the safe monkey-patching machinery wrapt provides.

```{note}
wrapture is in alpha ahead of 1.0.0, with pre-releases published to
[PyPI](https://pypi.org/project/wrapture/). Until 1.0.0 is final, a
plain `pip install wrapture` picks up the latest pre-release
automatically, so there is no need to pin a specific version. The API
is complete for the three uses described below and is not foreseen to
break, and the recording path has been through a performance pass, so
code written against it today is expected to carry forward to 1.0.0.
What the alpha series needs now is use: `unittest.mock` and
OpenTelemetry's own instrumentation are the established tools for the
two halves of what wrapture does, and the open question is whether an
alternative doing both from one mechanism is something people want.
Reports of it working, or not, on real code, and of what confused or
was missing, are what will decide whether anything changes before a
beta.
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
   optionally intervene (stub, transform, fail-inject). Unlike a
   `unittest.mock` `Mock`, which fabricates values and cannot see calls an object makes to itself,
   wrapture watches the real code run, and when a test must supply a
   stand-in it provides strict, recorded ones: `stub()` for a callable,
   spec-required `mock()` for a collaborator.

3. **Ad-hoc tracing.** Attach bindings to a running application, including
   one you cannot modify or redeploy, and emit a structured, nested trace to
   process or chart elsewhere. Name a handful of methods and a call tree
   appears; no code changes required: with a `wrapture.toml` naming the
   methods and a sink, `python -m wrapture manage.py runserver` traces the
   application untouched. With [autowrapt](https://github.com/GrahamDumpleton/autowrapt)
   installed, not even the launcher is needed: `AUTOWRAPT_BOOTSTRAP=wrapture`
   in the environment applies the same config at interpreter startup, so the
   program starts with plain `python`.

On top of the tracing layer sits
[OpenTelemetry export](otel-export.md): the same recorded events sent
to any OTLP backend as traces, metrics and correlated logs, switched
on by one `[otel]` table in the config, with the trace identity
arriving and leaving in W3C `traceparent` headers so two observed
services join one distributed trace. These are layers of one
mechanism, not separate products: the binding vocabulary that stubs a
method in a test is the same one that traces it in production, and
the config that names methods for a printed call tree is the config
that exports spans, so what starts as a monkey patch or a test
assertion can grow into full observability without the code being
rewritten along the way.

The distinction that matters: most instrumentation, OpenTelemetry's
own included, either has to be written into the code as SDK calls, or
arrives as auto-instrumentation covering only the frameworks it
already knows. wrapture needs neither: you point at your own methods
by name and a trace appears, and the same pointing is how it lands in
a test, a terminal, or a backend.

## At a glance

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

## How it was built

wrapture's code and documentation were written by an AI assistant under
the direction of Graham Dumpleton, the author of wrapt, through a long
process of specification, layered implementation, and validation
against real-world test suites. [How wrapture was built](how-wrapture-was-built.md)
explains the process and the thinking, so you can judge the provenance
with the facts in hand.

## Documentation

The guides are organised by mechanism: bindings and behaviours, then
recording in tests, then tracing a running process. The
[design concepts](design-concepts.md) page opens the section and is the recommended
first read: every idea the guides build on, a paragraph apiece, and
how they fit together. The worked examples
are organised by the question you arrive with, and each one combines
several of those mechanisms on a small concrete scenario, building up
from the problem to a finished test or configuration.

```{toctree}
:maxdepth: 2
:caption: Start here

getting-started
design-philosophy
coming-from-mock
how-wrapture-was-built
```

```{toctree}
:maxdepth: 2
:caption: Guides

design-concepts
monkey-patching
unit-testing
ad-hoc-tracing
wsgi-tracing
asgi-tracing
scheduled-tracing
otel-export
instrumentation-packages
```

```{toctree}
:maxdepth: 2
:caption: Worked examples

example-external-services
example-supplied-stand-ins
example-resource-hygiene
example-streaming-data
example-async-code
example-request-timing
example-service-over-time
example-third-party-libraries
example-pinning-configuration
```

```{toctree}
:maxdepth: 2
:caption: Reference

known-limitations
release-notes
```

## Source, issues and releases

wrapture is developed on GitHub at
[GrahamDumpleton/wrapture](https://github.com/GrahamDumpleton/wrapture),
under the BSD 2-Clause licence. Bug reports and feature requests go to
the [issue tracker](https://github.com/GrahamDumpleton/wrapture/issues);
a report is easiest to act on when it names the wrapture and Python
versions and includes a small binding that reproduces the problem.
Releases are published to [PyPI](https://pypi.org/project/wrapture/),
and each page of these docs carries an "Edit on GitHub" link to its
source under `docs/` in the repository, so a documentation fix can be
raised the same way.
