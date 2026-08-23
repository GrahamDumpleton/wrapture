# Writing an instrumentation package

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
class data for everything static and two methods for behaviour:

```python
# wrapture_instrumentation_flask/__init__.py
import wrapture


class FlaskInstrumentation(wrapture.Instrumentation):
    """Request, view and blueprint tracing for Flask applications."""

    target = "flask"
    supports = ">=2.0,<4"
    modules = ("flask.app", "flask.blueprints")
    requires = ("werkzeug",)
    removable = True
    settings = {
        "capture_headers": wrapture.Setting(False, "record request headers"),
        "ignore_paths": wrapture.Setting((), "paths never traced, exact match"),
    }

    def apply(self, name, module):
        from . import hooks

        hooks.instrument(name, module, self)
```

Reading down:

- `target` is the one top-level import name the class covers. Every
  entry in `modules`, the trigger modules whose import calls
  `apply()`, must live under it; wrapture refuses a class that claims
  a module outside its target the moment the class is defined.
- `supports` is a PEP 440 specifier against the target's installed
  version, read from package metadata. Outside the range, nothing
  registers and the user sees a `ConfigWarning`, never an error: the
  environment being newer or older than the package is not a
  misconfiguration. `modules` may be a mapping carrying a per-module
  specifier for a module that only exists from some version on,
  `{"flask.app": None, "flask.sansio.app": ">=2.3"}`; the dry run
  `python -m wrapture.tools instrumentation --verbose` shows what would
  register in a given environment. Version segmentation beyond that is
  the class's own dispatch on `self.target_version` inside `apply()`;
  wrapture gates and reports, it never selects among hook functions.
- `requires` names other targets that must have an enabled
  instrumentation in the same config. Requirements are not pulled in
  automatically; a missing one is a loud `ConfigError` naming the
  target.
- `removable` is the claim that the class can undo itself. It defaults
  to false, so say it; it governs `report()` and the warning
  `revert()` gives, and undo callbacks run either way.
- `settings` declares every key an `[[instrument]]` entry may carry,
  each a `Setting(default, description)`. An unknown key, or a value
  whose outer type does not match the default's, is a `ConfigError`
  when the config loads; the resolved values are `self.settings`
  inside the hooks. The description is what the listing tool and the
  generated template show beside each setting, so write it for the
  person editing the file.
- `name`, `description` and `version` default from the entry point
  name, the distribution's summary and the distribution's version.
  Set them only to override.

`apply(name, module)` is called once per trigger module, when that
module is imported or immediately if it already was, with the trigger
name so one class serving several modules can dispatch. `self` is
wrapture's per-application record: `self.settings`,
`self.target_version`, `self.applied` and `self.pending` (the
triggers fired and not yet fired), `self.trigger` (the one firing on
this thread), and `self.on_remove(callback)`. `configure()` is the
optional one-time hook before any trigger fires; `__init__` is
wrapture's and is not overridden.

## Import only wrapture

The module that defines the class imports wrapture and nothing else.
Anything that touches the target, the hook code, the framework's own
classes, lives behind an import inside `apply()` and `remove()`, the
`from . import hooks` above. The reason is the ordering everything
else depends on: wrapture loads the class when the config loads, so
that it can validate settings and report, and that happens before the
application imports anything. A class whose module imported `flask`
at the top would drag Flask in right then, ahead of the hook meant to
fire on its import, and the patches would land after the import they
were meant to precede. wrapture watches for exactly this: loading a
class is wrapped in a snapshot of `sys.modules`, and if any of the
class's own triggers (or its target) appeared, a `ConfigWarning` says
so, and the listing tool shows it as a warning line.

The same rule covers a multi-target package: each class's module
imports only wrapture, and does not import a sibling class's target
either.

## Removal: the bindings recipe

Two styles on one mechanism. The usual one is to register undo
callbacks from inside `apply()` with `on_remove()`, tagged
automatically with the trigger being applied, and let the base
`remove()` run them, most recent first, continuing past one that
raises. For instrumentation built on bindings, the recipe is three
lines:

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

    instrumentation.on_remove(group.remove)
```

Build the group, apply it, register its `remove()`. A `Binding`'s or
`BindingGroup`'s `remove()` returns the object, and `on_remove()`
ignores the return value, so the method passes straight in. The
alternative is overriding `remove(name, module)` for a centralised
teardown, when undo does not decompose per trigger.

wrapture calls `remove()` only for triggers whose `apply()` actually
ran, in reverse order on `revert()`. An `apply()` that raises has the
callbacks it registered before raising run at once, so its partial
work does not linger.

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
flask = "wrapture_instrumentation.flask:FlaskInstrumentation"
werkzeug = "wrapture_instrumentation.werkzeug:WerkzeugInstrumentation"
requests = "wrapture_instrumentation.requests:RequestsInstrumentation"
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
- `wrapture-instrumentation-<collection>` is a third-party
  multi-target package, where `<collection>` is a vendor, organisation
  or theme name that is not itself a Python package,
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
tests construct the class, call `apply()` on an imported module, and
call `remove()` afterwards, with none of wrapture's hook machinery
involved:

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
also assert that a bad setting is refused. For the whole path through
wrapture, the [unit testing guide](unit-testing.md#applying-instrumentation-in-a-test)
shows `wrapture.instrumentation(FlaskInstrumentation, ...)` scoping
an application of the class to a block, with `timeline()` recording
what its bindings observe.

## Checking an environment

Before publishing, and whenever a user reports a surprise, the
listing tool reads the class data the way wrapture will:

```console
$ python -m wrapture.tools instrumentation --verbose
flask  (wrapture-instrumentation-flask 1.2.0)
  Request, view and blueprint tracing for Flask applications
  target: flask 3.1.0, supported (>=2.0,<4)
  modules: flask.app, flask.blueprints
  requires: werkzeug
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
