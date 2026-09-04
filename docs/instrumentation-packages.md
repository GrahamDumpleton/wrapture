# Instrumentation packages

An instrumentation package ships, for one or more target packages,
the code that patches them on wrapture's behalf: a Flask
instrumentation that installs the recording middleware on every
application and observes every view, a requests instrumentation that
records each outbound call and propagates the trace identity. The
[ad-hoc tracing guide](ad-hoc-tracing.md#instrumentation-code-that-patches-a-target)
explains what an instrumentation is from the config file's side; this
page is the author's side: the class, the entry point, the rules that
keep it safe to load, how to test it, and how to name it.

## The shape

One class per target, subclassing `wrapture.Instrumentation`, with
class data for everything static and one decorated hook method per
trigger module:

```python
# wrapture_instrumentation_flask/__init__.py
import wrapture

from . import hooks


class FlaskInstrumentation(wrapture.Instrumentation):
    """Request, view and blueprint tracing for Flask applications."""

    target = "flask"
    supports = ">=2.0,<4"
    removable = True
    settings = {
        "capture_headers": wrapture.Setting(False, "record request headers"),
        "ignore_paths": wrapture.Setting((), "paths never traced, exact match"),
    }

    @wrapture.instrumentation_hook("flask.app")
    def flask_app(self, name, module):
        hooks.instrument(name, module, self)
```

Reading down:

- `target` is the import path of the module tree the class covers:
  a top-level name (`flask`) in the common case, or a dotted path
  when the unit of instrumentation is a submodule (`http.client`,
  `azure.storage.blob`). Every trigger module a hook declares must
  live at or under it; wrapture refuses a class that claims a module
  outside its target the moment the class is defined. Two enabled
  entries whose targets overlap, the same path or one inside the
  other, are a `ConfigError`, so an `http.client` class and an
  `http.server` class coexist while nothing is ever patched twice.
  A standard library module (`urllib.request`, `sqlite3`) is a
  target like any other: its version is the interpreter's own, so
  `supports` on such a class is a Python version range, and the
  listing says where the version came from. For anything else the
  version is the distribution that owns the path, resolved longest
  prefix first (`azure.storage.blob` is `azure-storage-blob`'s), so
  a namespace package's sibling distributions never stand in for
  each other.
- `supports` is a PEP 440 specifier against the target's installed
  version, read from package metadata. Outside the range, nothing
  registers and the user sees a `ConfigWarning`, never an error: the
  environment being newer or older than the package is not a
  misconfiguration. A trigger module that only exists from some
  version on carries its own specifier on its hook's decorator,
  `@wrapture.instrumentation_hook("flask.sansio.app", supports=">=2.3")`;
  the dry run `python -m wrapture.tools instrumentation --verbose`
  shows what would register in a given environment. Version
  segmentation beyond that is the hook's own dispatch on
  `self.target_version`; wrapture gates and reports, it never selects
  among hook functions.
- `requires` names other targets that must have an enabled
  instrumentation in the same config: a single name, or a sequence
  of them. Requirements are not pulled in automatically; a missing
  one is a loud `ConfigError` naming the target. It expresses a
  functional dependency between instrumentations, a class that is
  broken or incoherent unless another is also active, not the target
  package's own dependency graph: Flask depends on Werkzeug and
  Jinja2, but a Flask instrumentation is complete without either of
  theirs being enabled, so it must not require them. The example
  above sets nothing, and a well-shaped public class never needs to.
  The conventions make each one complete alone: every framework
  carries its own middleware, an event is fine as a root, and an
  annotation is a no-op when nothing is recording. The field exists
  for private instrumentation that trades those conventions away on
  purpose, one in-house target leaning on another's events or
  plumbing.
- `removable` is the claim that the class can undo itself. It defaults
  to false, so say it; it governs `report()` and the warning
  `revert()` gives, and cleanup callbacks run either way. A hook that
  cannot undo its own patches overrides the claim for its trigger
  alone, `@wrapture.instrumentation_hook(..., removable=False)`, and
  the class-level claim consumers see is then true only for the
  triggers that keep it.
- `settings` declares every key an `[[instrument]]` entry may carry,
  each a `Setting(default, description)`. An unknown key, or a value
  whose outer type does not match the default's, is a `ConfigError`
  when the config loads; the resolved values are `self.settings`
  inside the hooks. The description is what the listing tool and the
  generated template show beside each setting, so write it for the
  person editing the file.
- `name` and `version` default from the entry point name and the
  distribution's version; set them only to override. `description`
  defaults from the distribution's summary, which describes the whole
  collection, so a class in a multi-target package should set its
  own.

## Hooks

Each method decorated with `@wrapture.instrumentation_hook(module)`
is the hook for one trigger module: it is called as
`method(self, name, module)` when that module is imported, or
immediately if it already was, with the trigger's name and the module
object. The trigger string appears only on the decorator; the
class's trigger set is derived from its decorated methods, and the
method name itself is free. `self` is wrapture's per-application
record: `self.settings`, `self.target_version`, `self.applied` and
`self.pending` (the triggers fired and not yet fired), `self.trigger`
(the one firing on this thread, the same value as `name`), and
`self.on_cleanup(callback)`. `configure()` is the optional one-time
hook before any trigger fires; `__init__`, `apply()` and `remove()`
are wrapture's and are not overridden.

One method can serve several triggers by stacking the decorator,
which is why the signature always takes `name`:

```python
    @wrapture.instrumentation_hook("celery.app.task")
    @wrapture.instrumentation_hook("celery.app.base")
    def celery_app(self, name, module):
        ...
```

The base class owns `apply(name, module)` and `remove(name, module)`:
wrapture's dispatch calls them as triggers arrive, and a package's
own tests call them directly, with identical behaviour, which is
what makes the direct testing recipe below work.

## Import posture

The module that defines the class must not import the target it
patches. wrapture loads the class when the config loads, so that it
can validate settings and report, and that happens before the
application imports anything; a class whose module imported `flask`
at the top would drag Flask in right then, ahead of the hook meant to
fire on its import, and the patches would land after the import they
were meant to precede. wrapture watches for exactly this: loading a
class is wrapped in a snapshot of `sys.modules`, and if any of the
class's own triggers (or its target) appeared, a `ConfigWarning` says
so, and the listing tool shows it as a warning line.

Where the patch code lives is a matter of size, not of rules. A
small instrumentation, a couple of bindings built against the module
handed in, reads best directly in the hook method's body, as the
examples on the [ad-hoc tracing](ad-hoc-tracing.md#instrumentation-code-that-patches-a-target)
and [WSGI tracing](wsgi-tracing.md#framework-instrumentation-under-the-covers)
pages do. Once the patching grows past what one method holds
comfortably, the convention is a `hooks` module beside the class,
imported at the top as the skeleton above does. Both placements are
safe for the same reason: the code needs no target import of its
own, importing only wrapture at top level and receiving the trigger
module as a parameter, which for most instrumentation is everything
it touches.

When that stops being true, because the patching needs another
submodule of the target, a class the trigger module does not expose,
or a pile of helper modules, the imports must not ride on loading
the class. The options, in the order to reach for them:

- Import inside the hook function that uses the module, next to the
  use. The import runs when the trigger fires, by which time the
  target is imported anyway, so nothing is dragged in early.
- From Python 3.15 on, the language-level lazy import can sit at the
  top of `hooks.py` and defer just the same, keeping the imports in
  the conventional place.
- On older Pythons, `wrapt.lazy_import()` gives the equivalent: a
  module handle at the top of `hooks.py` that imports for real on
  first use, useful when the hook code needs many modules and
  function-local imports would repeat everywhere.

The same rule covers a multi-target package: each class's module
must not import a sibling class's target either.

## Removal: the bindings recipe

Two styles on one mechanism. The usual one is to register cleanup
callbacks from inside the hook with `on_cleanup()`, tagged
automatically with the trigger being applied, and let removal run
them, most recent first, continuing past one that raises. For
instrumentation built on bindings, the recipe is three lines:

```python
# wrapture_instrumentation_flask/hooks.py
import wrapture


def instrument(name, module, instrumentation):
    constructor = wrapture.binding(module.Flask, "__init__", when=False)
    constructor.on_call.decorates(wrap_app)

    registrar = wrapture.binding(module.Flask, "add_url_rule", when=False)
    registrar.on_call.transforms_args(wrap_view)

    group = wrapture.bindings(constructor=constructor, registrar=registrar)
    group.apply()

    instrumentation.on_cleanup(group.remove)
```

Build the group, apply it, register its `remove()`. A `Binding`'s or
`BindingGroup`'s `remove()` returns the object, and `on_cleanup()`
ignores the return value, so the method passes straight in. The
alternative, for teardown that does not decompose into callbacks, is
a cleanup method paired with the hook:

```python
    @wrapture.instrumentation_hook("flask.app")
    def flask_app(self, name, module):
        ...

    @flask_app.cleanup
    def remove_flask_app(self, name, module):
        ...
```

The paired method covers every trigger its hook claims. On removal
of a trigger, the `on_cleanup()` callbacks registered during its hook
run first, most recent first, then the paired cleanup method; both
continue past a raise with a warning.

wrapture removes only triggers whose hook actually ran, in reverse
order on `revert()`. A hook that raises has the callbacks it
registered before raising run at once, so its partial work does not
linger.

Removing a binding restores the patched location and deactivates the
wrapper, so a copy of it that the library or an application took by
from-import while the instrumentation was applied goes quiet rather
than recording on; the binding's `removed_calls` says whether that
happened. A library's own from-imports create such copies too, when
a parent or sibling module pulls a function out of the module that
defines it, so an instrumentation should be mindful of where a
target is re-exported and choose the trigger accordingly: patching
once the copying is complete, which may mean triggering on the
package root rather than the defining module.

The bindings a hook applies are reachable by a test of the
application running under the instrumentation, through
`wrapture.find_binding()` by location or label (see
[finding a binding applied elsewhere](unit-testing.md#finding-a-binding-applied-elsewhere)).
A label worth assigning is one a test would want to name, since the
alternative is the derived `module:qualname` path; and a test that
finds the binding gets the real one, so anything it reconfigures on
it stays reconfigured until the instrumentation removes it.

## When the target is a C extension

A binding needs an attribute it can replace, and a type implemented
in C has none: assignment onto `sqlite3.Connection` or
`sqlite3.Cursor` raises the `TypeError` that
[known limitations](known-limitations.md#builtin-and-extension-types-cannot-be-patched)
describes. What such a library does have is a Python-reachable
factory: some function hands the C objects out, and that function is
bindable. The pattern is to bind the factory, wrap what it returns
in a proxy class of your own, and bind the proxy's methods, which
are plain Python methods you own. Everything the library's users do
with the object flows through your class, so the whole binding
vocabulary applies to a type that could never be patched directly:

```python
# wrapture_instrumentation_sqlite3/dbapi2.py
import wrapt

import wrapture


class Cursor(wrapt.BaseObjectProxy):
    """A recording proxy around sqlite3.Cursor: the methods worth
    recording are overridden for binding, everything else delegates."""

    def execute(self, sql, parameters=(), /):
        outcome = self.__wrapped__.execute(sql, parameters)

        return self if outcome is self.__wrapped__ else outcome

    # A sqlite3 cursor is its own iterator. Special methods are
    # looked up on the type, and BaseObjectProxy leaves them to the
    # subclass, so both halves are written out explicitly.

    def __iter__(self):
        return self

    def __next__(self):
        return self.__wrapped__.__next__()


class Connection(wrapt.BaseObjectProxy):
    """A recording proxy around sqlite3.Connection."""

    def cursor(self, *args, **kwargs):
        return Cursor(self.__wrapped__.cursor(*args, **kwargs))


def instrument(name, module, instrumentation):
    def opens(wrapped, instance, args, kwargs):
        return Connection(wrapped(*args, **kwargs))

    connect = wrapture.binding(
        module, "connect", leaf=True, category="database"
    )
    connect.on_call.decorates(opens)

    execute = wrapture.binding(
        Cursor,
        "execute",
        label="sqlite3:Cursor.execute",
        leaf=True,
        category="database",
    )

    group = wrapture.bindings(connect=connect, execute=execute)
    group.apply()

    instrumentation.on_cleanup(group.remove)
```

The factory binding does two jobs at once: it records the
construction as an event of its own, and its decorator substitutes
the proxy, so every connection the application obtains after apply
is a recording one, cursors included. The `execute` binding then
lands on the proxy class, where `when=`, capture policies, `leaf=`
and `category=` all behave exactly as they would on a patchable
target.

Three rules keep the proxy honest:

- Derive from `wrapt.BaseObjectProxy` and write the special methods
  you need explicitly. Dunder methods are looked up on the type, not
  the instance, and the base proxy deliberately does not forward
  them, so each one is an opt-in. That explicitness is the point: a
  base class that forwarded `__iter__` wholesale would make wrapped
  objects appear iterable whether or not the real one was.

- Preserve the library's identity conventions. Where the wrapped
  method returns the wrapped object, return the proxy instead, so a
  chained `cursor.execute(...).execute(...)` stays on the recording
  class; a context manager whose `__enter__` returns the raw object
  substitutes `self` for the same reason.

- Label the bindings with the names they stand in for. The derived
  path of the `execute` binding is your proxy's `module:qualname`,
  which is true but not what a reader of the trace wants; the label
  `sqlite3:Cursor.execute` carries the name the method notionally
  wraps, exactly the job labels exist for.

Removal is unchanged, and answers the question the limitation would
otherwise leave open. The bindings are on your classes, so
`group.remove()` restores them cleanly, and the factory binding's
removal stops new connections being wrapped. A connection created
while the instrumentation was applied keeps its proxy for its own
lifetime, but a proxy whose bindings are gone is pure passthrough
and records nothing.

This is also the one place an instrumentation package imports wrapt
directly. wrapture deliberately does not re-export the proxy types:
the `import wrapt` is a visible marker that the code has stepped
below wrapture's binding vocabulary, and since wrapt is a dependency
of wrapture it is always present. The
[sqlite3 target](https://github.com/GrahamDumpleton/wrapture-instrumentation/blob/develop/src/wrapture_instrumentation/database/sqlite3/README.md)
in wrapture-instrumentation is the full-scale form of this example:
the whole execute family on both classes, the commit-or-rollback
context manager, and the capture policy decisions that go with
recording SQL.

## Shaped settings

The outer-type check on settings is deliberately shallow: it catches
a string where an integer was wanted and a scalar where a list was,
and nothing inside a list or table, because element types cannot be
inferred from an empty default. A setting with a shape of its own is
checked in `configure()`, which runs once before any trigger fires,
so a `ConfigError` raised there still surfaces at config time:

```python
class FlaskInstrumentation(wrapture.Instrumentation):
    ...
    settings = {
        "routes": wrapture.Setting((), "routes to trace, each {path, methods}"),
    }

    def configure(self):
        for route in self.settings["routes"]:
            if not isinstance(route, dict) or "path" not in route:
                raise wrapture.ConfigError(
                    f"routes: each entry needs a path, got {route!r}"
                )
```

wrapture guarantees `routes` is a list before `configure()` runs; the
class guarantees the rest.

## Declaring what a target is

An instrumentation knows two structural things about its target
that a config author cannot be asked to work out: whether the
target's operations are worth subdividing, and what kind of
operations they are. Both are declared on the binding (or on the
`observed()` callable or `block()` the package substitutes), decided
before any event exists rather than during the call:

```python
client = wrapture.binding(module.Client, "request", leaf=True, category="external")
```

`leaf=True` makes the entry point a terminal node: its event records
and covers everything the call did, and nothing that would make a
span records beneath it, so a client's internal HTTP requests,
connection handling and retries stay out of the tree while its log
lines still attach to the leaf. `category=` names the kind of
operation, one of the categories this package layout is organised
by (`external`, `database`, `datastore`, `messaging`, `task`,
`server`, `consumer`, `template`), so that a `database` target's
events say `database` with no translation. The category is also the
layout convention inside a package: a package instrumenting a single
target names its subpackage `<category>_<target>` with the target's
module dots as underscores (`external_requests`,
`external_urllib_request`), and a collection covering many targets
groups them in role directories instead, `<category>/<target>`
(`framework/flask`, `external/urllib_request`,
`server/xmlrpc_server`), the same words with the role as a
directory. Either way the layout is internal; the entry point name
is always the bare target. The [ad-hoc tracing
guide](ad-hoc-tracing.md#terminal-nodes-leaf-and-category) covers
both, and the [OTel page](otel-export.md#the-category-data-key-contracts)
lists the data keys each category is expected to carry; the
instrumentation fills those with `annotate()` from inside the
operation, which lands on the leaf, or with `data=` for a value
fixed for the target. A `url` carries no query string; the query
goes under `query` through `wrapture.capture_query()`, which records
it the way the request middlewares do, the built-in sensitive names
redacted and any `redact()` names the instrumentation's own setting
adds on top. Offer a per-target setting (`leaf`, default true) so a
user debugging the client itself can see its internals.

One seam sometimes fronts several kinds of operation: an SDK
client's single dispatch method reaches object storage, a queue and
a function service depending on the client it was called on, and it
can only be bound once. There the honest declaration is a rule, and
`category=`, `label=` and `data=` each accept a callable with the
`when=` signature in place of the value, consulted per operation
after `when=` has accepted it and before the event is built, so the
category, the low-cardinality name (`s3/GetObject`) and the tags the
arguments already say are all decided from the call and a behaviour
handler then only annotates what the outcome says. The [ad-hoc
tracing guide](ad-hoc-tracing.md#deciding-the-name-kind-and-tags-per-operation-resolvers)
has the contract. The category a resolver answers should still be
one of the words above, and the label repeatable, never an
identifier.


One contract follows from leaves and clients composing: propagation
belongs to the level that records. A client instrumentation that
injects `wrapture.trace_headers()` into what it sends must gate the
injection on its own binding having recorded, which is one check
away:

```python
if wrapture.current_event(binding=client):
    ...  # inject the trace identity
```

Beneath another target's leaf the binding is silenced, behaviour
still running but no event recorded, and the check comes back empty:
nothing is injected, because the leaf either propagates at its own
level (as every packaged client does for itself) or has chosen, by
not injecting, that the service beneath it is not part of the trace,
a third-party API that would not understand the headers and should
not be handed the tree's identity. The `annotate()` half needs no
gate: inside a silenced call an unaimed `annotate()` lands nowhere,
never on the leaf's event, so the leaf's own story stands whatever
the client beneath it says about itself.

## Registering the class

The entry point group is `wrapture.instrumentation`, one entry per
class, the entry point name being the instrumentation's name. The
group name has a dot in it, so the table header in `pyproject.toml`
has to be quoted:

```toml
[project]
name = "wrapture-instrumentation-flask"
version = "1.2.0"
description = "Request, view and blueprint tracing for Flask applications"
dependencies = ["wrapture"]

[project.entry-points."wrapture.instrumentation"]
flask = "wrapture_instrumentation_flask:FlaskInstrumentation"
```

Note what is not in `dependencies`: Flask. An instrumentation package
depends on wrapture, never on its target, because installing the
instrumentation must not install the thing it instruments, and the
version gate is `supports`, checked at apply time against whatever
the environment has.

An entry point value is a single object reference, so a package
covering several targets registers several entries, one per class,
each named for its target:

```toml
[project.entry-points."wrapture.instrumentation"]
flask = "wrapture_instrumentation.framework.flask:FlaskInstrumentation"
werkzeug = "wrapture_instrumentation.framework.werkzeug:WerkzeugInstrumentation"
requests = "wrapture_instrumentation.external.requests:RequestsInstrumentation"
```

One distribution can therefore ship instrumentation for many common
packages without being many packages, and the config's side sees no
difference: each class is still one target, one trigger set, one
`[[instrument]]` entry, switched on by name. Installing the package
registers the classes and applies none of them; nothing patches a
target until a config entry names it.

The config names a registered instrumentation by its bare entry point
name, `name = "flask"`, when exactly one installed distribution
registers it, and as `name@distribution`
(`requests@wrapture-instrumentation-acme`) when two do. The part after
the `@` is the distribution name, matched after the usual
normalisation, which is why entry point names should always be the
bare target and never vendor-prefixed: the qualifier is the
distribution, and `name = "requests"` then means the same thing
whichever package provides it.

## Naming the package

Only two things matter mechanically: a common distribution prefix, so
instrumentation is findable on the index and in `pip list`, and a
distinct import package per distribution, so two installed packages
never clobber each other. The `name@distribution` qualifier covers
what no convention can, two packages for one target. With that:

- `wrapture-instrumentation` is the project's own multi-target
  package.

- `wrapture-instrumentation-<target>` is a package covering exactly
  one target, `wrapture-instrumentation-flask`. First publisher gets
  the name, as with `pytest-<name>`; nothing is reserved.

- `wrapture-instrumentation-<collection>` is a multi-target package
  named for something that is not itself a Python package: a vendor,
  product, organisation or theme. The project's own companion
  packages use this form for targets that need a backend to test
  against (`wrapture-instrumentation-aws` for the AWS SDK,
  `wrapture-instrumentation-postgresql` for the PostgreSQL client
  libraries), and so does a third party publishing its own
  collection,
  `wrapture-instrumentation-acme`; the qualified name then reads
  `requests@wrapture-instrumentation-acme`.

- Entry point names are always the bare target, `requests`, never
  vendor-prefixed.

- The import package is `wrapture_instrumentation` for the project's
  own and `wrapture_instrumentation_<suffix>` for everyone else, one
  per distribution, not a namespace package shared across
  distributions.

There is no `contrib` segment: everything not the project's own is
contrib, so the word carries no information and lengthens every
third-party name.

## Testing the class directly

Testability is a deliberate property of the shape. A package's own
tests construct the class, call `apply()` with a trigger name and the
imported module, and call `remove()` afterwards, with none of
wrapture's hook machinery involved; these are the same methods
wrapture's own dispatch calls, so the two paths cannot drift apart:

```python
import flask
from wrapture_instrumentation_flask import FlaskInstrumentation


def test_requests_are_recorded():
    instrumentation = FlaskInstrumentation(capture_headers=True)
    instrumentation.apply("flask.app", flask.app)
    try:
        ...
    finally:
        instrumentation.remove("flask.app", flask.app)
```

Constructing the class runs the settings validation, so a test can
also assert that a bad setting is refused. Applying a trigger the
class does not declare, or one already applied, is a `ConfigError`;
removing one that never applied is a no-op. For the whole path through
wrapture, the [unit testing guide](unit-testing.md#applying-instrumentation-in-a-test)
shows `wrapture.instrumentation(FlaskInstrumentation, ...)` scoping
an application of the class to a block, with `timeline()` recording
what its bindings observe; its `triggers=` keyword scopes the
application to a subset of the declared triggers, so a multi-trigger
class can be tested one hook at a time.

## Checking an environment

Before publishing, and whenever a user reports a surprise, the
listing tool reads the class data the way wrapture will:

```console
$ python -m wrapture.tools instrumentation --verbose
flask  (wrapture-instrumentation-flask 1.2.0)
  Request, view and blueprint tracing for Flask applications
  target: flask 3.1.0, supported (>=2.0,<4)
  modules: flask.app, flask.blueprints
  removable: yes
  settings:
    capture_headers = false   record request headers
    ignore_paths = []         paths never traced, exact match
  would register: flask.app
  would register: flask.blueprints
  url: https://example.org/wrapture-instrumentation-flask
```

A class that cannot load shows its error in place, and one whose
module imported its own target shows the warning described above.
`--toml` writes the `[[instrument]]` template a user would paste into
their file, every entry disabled and every setting commented out at
its default, which is also a quick check that the descriptions read
well where they will be read.

## The wrapture-instrumentation package

Everything above is the contract for writing an instrumentation
package; [wrapture-instrumentation](https://github.com/GrahamDumpleton/wrapture-instrumentation)
is the package built on it, maintained alongside wrapture itself. It
provides instrumentation for a growing range of targets across the
categories an application is built from: web frameworks (Django,
Flask, FastAPI, Starlette, each recording requests, views and
failures as one tree per request), the servers that carry them
(uvicorn, aiohttp.web, the werkzeug and wsgiref development servers,
xmlrpc.server), outbound HTTP and RPC clients (requests, httpx,
urllib3, aiohttp's client, gRPC and the standard library's own,
propagating trace identity hop by hop), databases (SQLAlchemy and
sqlite3, queries and transaction boundaries with parameters never
recorded) and template engines (Jinja2).

```console
$ pip install wrapture-instrumentation
```

installs the entries; enabling one is an `[[instrument]]` entry in
the config file, or `wrapture.instrumentation("flask", "jinja2")` in
code. What each target records, its settings and its capture
decisions are documented in a per-target README inside the package,
linked from the
[project README](https://github.com/GrahamDumpleton/wrapture-instrumentation#readme),
and the listing tool above enumerates whatever is installed.

Targets that need a separate product or service behind them to test
against are kept out of that package and come as companion packages
under the same convention, each with its own test arrangements and
release cadence. The first is
[wrapture-instrumentation-aws](https://github.com/GrahamDumpleton/wrapture-instrumentation-aws),
covering the AWS SDK (boto3 and botocore) through the one `botocore`
entry point: every AWS API call as one event named
`service/operation` and categorised per service (DynamoDB a
datastore, SQS, SNS and Kinesis messaging, Lambda and Step Functions
tasks, S3 and the rest external), the name, category and tags decided
per call by [resolvers](ad-hoc-tracing.md#deciding-the-name-kind-and-tags-per-operation-resolvers)
on a single binding at the SDK's one dispatch seam. The second is
[wrapture-instrumentation-postgresql](https://github.com/GrahamDumpleton/wrapture-instrumentation-postgresql),
covering the PostgreSQL client libraries through one entry point per
driver, `psycopg`, `psycopg2` and `asyncpg`: every query as a
`database` leaf however it was issued, the connection being opened
and each transaction boundary, sync and async alike, with the SQL
text recorded only when its `statement` setting is on and bound
parameters never; its test suite runs against a real PostgreSQL
server in a container, the arrangement the separate-package rule
exists for. Installed beside the core package each is enabled the
same way, and the listing tool shows them all.
