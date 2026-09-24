# Blog posts and workshops

Two sets of material sit beside these docs for learning wrapture by
doing. A series of blog posts each take one area of the library and
work through it, and a collection of guided workshops run in JupyterLab
and check your work as you go. These docs are the reference; the posts
and the workshops are the tour.

[![Launch on Binder](https://mybinder.org/badge_logo.svg)](https://mybinder.org/v2/gh/GrahamDumpleton/wrapture-workshops/main?urlpath=lab)
[![Open in GitHub Codespaces](https://img.shields.io/badge/launch-codespaces-579ACA?logo=github&logoColor=white)](https://codespaces.new/GrahamDumpleton/wrapture-workshops?quickstart=1)

The workshops need nothing installed. The Binder badge above, or
[this link](https://mybinder.org/v2/gh/GrahamDumpleton/wrapture-workshops/main?urlpath=lab),
starts them on [mybinder.org](https://mybinder.org), a free public
service that builds the workshops repository into a temporary
JupyterLab running in your browser. Building takes a minute or two.
A session is discarded when it ends, so finish a workshop in the
session you started it in, and shut the session down when you are
done rather than closing the tab.

The Codespaces badge, or
[this link](https://codespaces.new/GrahamDumpleton/wrapture-workshops?quickstart=1),
starts them instead in [GitHub Codespaces](https://github.com/features/codespaces),
which builds the repository into a container of your own in the cloud
and opens it in VS Code in the browser, with JupyterLab served on a
forwarded port. It needs a GitHub account and uses your account's
Codespaces allowance. Unlike a Binder session, a codespace is kept
until you delete it, so your work survives between visits; delete it
from [github.com/codespaces](https://github.com/codespaces) when you
have finished with the workshops. The repository
[README](https://github.com/GrahamDumpleton/wrapture-workshops#launch-on-codespaces)
describes the codespace in full.

## Blog posts

The posts are on Graham Dumpleton's blog, collected on the guide page
[Testing and tracing with wrapture](https://grahamdumpleton.me/guides/testing-and-tracing-with-wrapture/),
which is the list that grows as posts are added. In reading order:

**Introduction**

- [Introducing wrapture](https://grahamdumpleton.me/posts/2026/08/introducing-wrapture/).
  What wrapture is, why it was built, and how it came to be written.

**Unit testing**

- [Unit testing with wrapture](https://grahamdumpleton.me/posts/2026/09/unit-testing-with-wrapture/).
  Testing by wrapping rather than replacing: the real code still runs,
  and everything that flows through it is recorded.

- [Recording calls with wrapture](https://grahamdumpleton.me/posts/2026/09/recording-calls-with-wrapture/).
  Timelines and the tape: what an event holds, and reading the record
  back through filters, assertions and expectations.

- [Phased behaviour in wrapture](https://grahamdumpleton.me/posts/2026/09/phased-behaviour-in-wrapture/).
  Scripting what a binding does over successive calls, for retry
  loops, circuit breakers and polling.

- [Beyond callables in wrapture](https://grahamdumpleton.me/posts/2026/09/beyond-callables-in-wrapture/).
  Bindings on attributes, values, mappings and generators, not only on
  functions and methods.

**Tracing**

- [Live tracing with wrapture](https://grahamdumpleton.me/posts/2026/09/live-tracing-with-wrapture/).
  Bindings with a sink and no timeline, so a running program narrates
  its own calls.

- [Zero-code tracing with wrapture](https://grahamdumpleton.me/posts/2026/09/zero-code-tracing-with-wrapture/).
  The same trace from a `wrapture.toml` beside an unchanged program,
  under `python -m wrapture` or injected at startup by autowrapt.

- [Tracing Flask with wrapture](https://grahamdumpleton.me/posts/2026/09/tracing-flask-with-wrapture/).
  Each HTTP request recorded as one tree from a single instrumentation
  entry in the config.

- [Finding slow code with wrapture](https://grahamdumpleton.me/posts/2026/09/finding-slow-code-with-wrapture/).
  Reading time off a call tree, telling slow from slow because of a
  child, and aggregate reports over many requests.

- [OpenTelemetry export in wrapture](https://grahamdumpleton.me/posts/2026/09/opentelemetry-export-in-wrapture/).
  The recorded events sent to an OpenTelemetry backend as spans and
  metrics, with one trace id carried across services.

## Workshops

The workshops live in
[GrahamDumpleton/wrapture-workshops](https://github.com/GrahamDumpleton/wrapture-workshops)
on GitHub. Each takes one thing you might want to do with wrapture and
walks you through doing it in a live JupyterLab, with the instructions
in a side panel whose actions drive the session (terminals, files, the
editor, notebooks and kernels) and check what you have done. A
workshop installs wrapture into a virtual environment of its own
inside the workshop directory, the way a project would, so nothing is
installed into the JupyterLab environment and nothing is left behind.

The workshops are written against a released version of wrapture,
pinned in the repository, so these docs may describe a newer version
than the one a workshop uses. A feature described here may not be in a
workshop until its pin moves.

Besides Binder and Codespaces, the workshops run locally under any
JupyterLab: the repository's
[README](https://github.com/GrahamDumpleton/wrapture-workshops#run-locally)
has the steps, with uv and with pip. The list below is in the order to
take them, with roughly how long each takes; the repository README
carries the fuller description of each.

1. **Your first binding** (`first-binding`, 10 minutes). Create a
   binding on a method, apply, suspend, resume and remove it, scope it
   to a block, and change one thing about a call while the real method
   runs.

2. **Testing by wrapping, not replacing** (`wrap-not-replace`,
   15 minutes). The same unit tests written with `unittest.mock` and
   with wrapture, ending with the decorator form and the pytest plugin.

3. **Recording what real code did** (`recording-calls`, 15 minutes).
   Record real calls on a timeline and read the tape back: events,
   filters, assertions, expectations, the call tree and the stack, and
   a record turned into a pytest test that fails until the code is
   fixed.

4. **Behaviour that changes over time** (`phased-behaviour`,
   15 minutes). Script a binding's behaviour with phases: a count for a
   retry loop, a condition for a circuit breaker, a sequence for a
   polling loop, and `advance()` from the test.

5. **Bindings that are not calls** (`beyond-callables`, 15 minutes).
   Bind an attribute, an environment variable, a module constant, a
   settings dict, a callable kept in a registry, and a generator.

6. **A program that narrates itself** (`live-tracing`, 10 minutes).
   Bindings with a `Printer` sink and no timeline, redaction, and
   narrowing the trace at the sink, at the binding and for a subtree.

7. **Tracing without touching the program** (`zero-code-tracing`,
   15 minutes). The bindings and the sink in a `wrapture.toml`, run
   under `python -m wrapture` and then injected by autowrapt; the
   trace kept as JSON Lines and drawn as a sequence diagram.

8. **Analysing a trace in a notebook** (`analysing-a-trace`,
   15 minutes). Three hundred orders under a JSON Lines sink treated as
   data: a DataFrame, the tree rebuilt from parent links, charts of
   where the time and the errors went, and latency by tenant.

9. **One request as one tree** (`tracing-flask`, 15 minutes). The shop
   behind Flask, each HTTP request recorded as one tree from a single
   `[[instrument]]` entry, with the health checks kept out.

10. **Where the time goes** (`finding-slow-code`, 15 minutes). Self
    time against total time, asserted on in a test, and an `Aggregate`
    report over thirty requests from a window in the config.

11. **The same events, sent to a backend** (`opentelemetry-export`,
    20 minutes). OpenTelemetry export switched on from one `[otel]`
    table and read from the console exporters, then one trace id
    across a client and a service.

12. **wrapture and pytest, properly** (`wrapture-with-pytest`,
    20 minutes). Scoping bindings in a suite, yield fixtures, shared
    declarations applied per test, the plugin's leak sweep and `tape`
    fixture, and a query budget from a counter.

13. **Converting a mock test suite** (`coming-from-mock`, 20 minutes).
    A `unittest.mock` test module converted to wrapture one idiom at a
    time, green after every step, and the one test to leave as mock.

14. **When the test must supply the callable** (`supplying-stand-ins`,
    15 minutes). Stubs and strict collaborator doubles for a pipeline
    whose transport and completion hook the test has to supply.

15. **Testing what a consumer does with a stream**
    (`streaming-and-generators`, 15 minutes). Consumers of a paginated
    catalogue tested against the real generator: one event per
    iteration, a proxy that sees every item, a failure injected at a
    chosen page, and items transformed on the way through.

16. **Async methods and generators** (`testing-async-code`,
    15 minutes). A notifier whose client is async all the way down:
    stubs whose outcome arrives on await, a timeout through the real
    loop, the coroutine that was never awaited, concurrent sends, an
    async generator, and the suite under pytest-asyncio.

17. **Changing what a library does** (`patching-third-party-code`,
    20 minutes). A vendored client patched without editing it: a
    header injected, the patch suspended and reconfigured, a retry
    around the call, a post-import hook, and the same patch from a
    config file.

18. **Messages, phases and handled failures as events**
    (`logs-blocks-and-notes`, 15 minutes). Log messages, named blocks,
    annotations and noted exceptions on the same tape as the calls,
    all inert when nothing is listening.

19. **Where events go** (`sinks-and-collectors`, 20 minutes). A sink of
    your own, what a process sink hears that a timeline cannot,
    fan-out, depth, filtering and sampling, `Counter` and `Aggregate`,
    terminal nodes with a category, and resolvers naming each event.

20. **Reading a trace after the fact** (`trace-files-and-tools`,
    15 minutes). JSON Lines as the durable form of a trace: streamed to
    disk from a config file, converted for Perfetto, rendered as a
    golden file a test compares the live tree against, and rotated on
    a schedule.

21. **Reports on a schedule** (`watching-a-service-over-time`,
    15 minutes). A `Window` with `every=` in code and then in the
    config: a summary each period, a report on demand from a signal, a
    JSON Lines stream that rotates rather than grows, and what a
    restart does to the schedule.

22. **One trace across two processes** (`distributed-tracing`,
    20 minutes). One trace id across a client and a server through the
    `traceparent` header, two JSON Lines files joined on the id, and
    deferred work linked back to its origin rather than nested under
    it.

23. **Requests in tests, and at the boundary** (`testing-web-requests`,
    15 minutes). The Flask instrumentation inside a pytest test, then
    the `on_request` namespace on a WSGI binding: a canned response, a
    fault the server sees, and a status rewritten on the way out.

24. **Instrumenting a package nobody has covered**
    (`writing-instrumentation`, 20 minutes). An `Instrumentation` class
    for a small library: a hook per trigger module, settings validated
    when the config loads, tested directly and through wrapture, and
    packaged with an entry point.
