# wrapture

**Wrap anything, capture everything, change nothing.**

[![Tests](https://github.com/GrahamDumpleton/wrapture/actions/workflows/build-test-release.yml/badge.svg?branch=develop)](https://github.com/GrahamDumpleton/wrapture/actions/workflows/build-test-release.yml)
[![Documentation](https://readthedocs.org/projects/wrapture/badge/?version=latest)](https://wrapture.readthedocs.io)

wrapture (`wrapt` + `capture`) is a Python library for attaching bindings to
arbitrary call sites, without modifying the code being observed, and doing
something useful with what flows through them.

It is a sibling project to [wrapt](https://github.com/GrahamDumpleton/wrapt)
and [autowrapt](https://github.com/GrahamDumpleton/autowrapt), building on the
safe monkey-patching machinery wrapt provides.

> **Status: alpha, ahead of 1.0.0.** Pre-releases are published to PyPI,
> and until 1.0.0 is final a plain `pip install wrapture` picks up the
> latest pre-release automatically, so there is no need to pin a specific
> version. The API is complete for the three uses described below and is
> not foreseen to break, and the recording path has been through a
> performance pass (the
> [OpenTelemetry export](https://wrapture.readthedocs.io/en/latest/otel-export.html#what-it-costs)
> guide puts numbers beside OTel's own instrumentation), so code written
> against it today is expected to carry forward to 1.0.0. What the alpha
> series needs now is use. `unittest.mock` and OpenTelemetry's own
> instrumentation are the established tools for the two halves of what
> wrapture does, and the open question is whether an alternative that
> does both from one mechanism is something people want. Reports of it
> working, or not, on real code, and of what confused or was missing,
> are what will decide whether anything changes before a beta; they go
> to the [issue tracker](https://github.com/GrahamDumpleton/wrapture/issues).

## Installation

wrapture is on [PyPI](https://pypi.org/project/wrapture/):

```console
$ pip install wrapture
```

or with uv:

```console
$ uv add wrapture
```

## Documentation

Full documentation is at [wrapture.readthedocs.io](https://wrapture.readthedocs.io).
Start with the [getting started](https://wrapture.readthedocs.io/en/latest/getting-started.html)
page: everything on it can be pasted into a Python interpreter. Coming
from `unittest.mock`? There is a
[comparison page](https://wrapture.readthedocs.io/en/latest/coming-from-mock.html)
mapping each mock idiom to its wrapture counterpart. After that, the
worked examples, starting with
[testing code that calls external services](https://wrapture.readthedocs.io/en/latest/example-external-services.html),
each take one question you might arrive with and answer it end to end.

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

The same bindings intervene as well as observe: stub a result, inject a
failure, or transform one argument while the real code keeps running.

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
   spec-required `mock()` for a collaborator. This makes it possible to test code
   with no injectable seams at all, and to assert on what *didn't* happen on
   an error path: inject a gateway timeout, then verify the ledger was not
   written, the receipt was not sent, and the compensating refund was issued.

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
[OpenTelemetry export](https://wrapture.readthedocs.io/en/latest/otel-export.html):
the same recorded events sent to any OTLP backend as traces, metrics and
correlated logs, switched on by one `[otel]` table in the config, with the
trace identity arriving and leaving in W3C `traceparent` headers so two
observed services join one distributed trace. These are layers of one
mechanism, not separate products: the binding vocabulary that stubs a method
in a test is the same one that traces it in production, and the config that
names methods for a printed call tree is the config that exports spans, so
what starts as a monkey patch or a test assertion can grow into full
observability without the code being rewritten along the way.

The distinction that matters: most instrumentation, OpenTelemetry's own
included, either has to be written into the code as SDK calls, or arrives
as auto-instrumentation covering only the frameworks it already knows.
wrapture needs neither: you point at your own methods by name and a trace
appears, and the same pointing is how it lands in a test, a terminal, or a
backend.

## Pre-built instrumentation

Pointing at your own methods is the core of wrapture, but for common
third-party packages the pointing has already been done. The companion
[wrapture-instrumentation](https://github.com/GrahamDumpleton/wrapture-instrumentation)
package provides ready-made instrumentation for popular Python packages
such as web frameworks and template engines (currently Flask and Jinja2,
with more targets to come), each recording a request or render as one
structured tree:

```console
$ pip install wrapture-instrumentation
```

Enabling a target is an `[[instrument]]` entry in `wrapture.toml`, or
`wrapture.instrumentation("flask", "jinja2")` in code, and it composes
with your own bindings in the same trace. The
[instrumentation packages](https://wrapture.readthedocs.io/en/latest/instrumentation-packages.html)
page describes how these packages work and how to write one for a
package not yet covered.

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
- APM agents are all-or-nothing products rather than a toolkit.

wrapture fills that gap: a targeted call tree with normalized arguments and
return values, produced by naming the methods you care about, usable as a
testing assertion library, a tracing tool, or both at once.

## What it is not

- **Not a fabrication tool.** There is no spec-less `Mock()` here by
  design; stand-ins are strict and built from named specs, and
  `unittest.mock` remains the tool for invented objects.
- **Not a production APM.** It is a toolkit that APM-like things could be
  built on.
- **Not an OpenTelemetry competitor.** It should emit to OTel, not replace
  it.

## How it was built

wrapture's code and documentation were written by an AI assistant under
the direction of Graham Dumpleton, the author of wrapt, through a long
process of specification, layered implementation, and validation against
real-world test suites.
[How wrapture was built](https://wrapture.readthedocs.io/en/latest/how-wrapture-was-built.html)
explains the process and the thinking.

## Requirements

- Python 3.12+
- [wrapt](https://github.com/GrahamDumpleton/wrapt) 2.4.0+

## Issues

Bug reports and feature requests go to the
[issue tracker](https://github.com/GrahamDumpleton/wrapture/issues).
Include the wrapture and Python versions and, where you can, a small
binding that reproduces the problem.

## License

BSD 2-Clause. See
[LICENSE](https://github.com/GrahamDumpleton/wrapture/blob/develop/LICENSE).
