# Pinning configuration for a test

A pricing function reads its configuration from everywhere
configuration usually lives: an API key in an environment variable, a
settings dict on a config module that other modules imported directly,
a module-level timeout constant, and a formatter looked up in a
registry dict by name. The function is what the test is for; the
configuration is what each test needs pinned to known values, put back
afterwards no matter how the test ends, and pinned in a way every
holder of the configuration sees, including code that grabbed a
reference at import time.

The stdlib answers are `unittest.mock.patch.dict` and pytest's
`monkeypatch`, one idiom per shape. wrapture spells all of them as
bindings, which buys the same lifecycle everywhere: each is a context manager, they group,
they can be suspended and resumed, and the pytest plugin's leak sweep
reports any left applied. And when the question shifts from "hold this
value" to "who reads this value", the same location upgrades from a
value binding to an interception binding that sees every read.

This example uses: [value bindings](monkey-patching.md#value-bindings-holding-a-value-in-place),
[mapping bindings](monkey-patching.md#mapping-bindings-substituting-a-mappings-content),
[module attributes](monkey-patching.md#attribute-bindings),
[binding modes](monkey-patching.md#binding-modes-call-versus-attribute) and
[recording on a timeline](unit-testing.md#recording-calls-on-a-timeline).

## The application code

The config module here is built with `types.ModuleType` and registered
in `sys.modules` so the example runs anywhere; read the next few lines
as your application's `config.py`, imported as any module would be.
The `price()` function reads all four shapes on every call:

```python
>>> import os, sys, types
>>> import wrapture

>>> config = types.ModuleType("config")
>>> sys.modules["config"] = config            # as `import config` would
>>> config.SETTINGS = {"currency": "USD", "tax_rate": 0.2}
>>> config.TIMEOUT = 30.0
>>> config.FORMATTERS = {"plain": lambda total: f"total={total:.2f}"}

>>> def price(amount: int, style: str = "plain") -> str:
...     if "API_KEY" not in os.environ:
...         raise RuntimeError("API_KEY is not configured")
...
...     total = amount * (1 + config.SETTINGS["tax_rate"])
...     formatter = config.FORMATTERS[style]
...
...     return f"[{config.SETTINGS['currency']} within {config.TIMEOUT}s] " + formatter(total)

>>> price(100)
Traceback (most recent call last):
    ...
RuntimeError: API_KEY is not configured

```

## The naive approach: assign and hope

Tests can assign to all of this directly and put it back in a
`finally`, and many suites do. The failure modes are well known: the
restore is skipped on the path nobody tested, a dict is replaced rather
than mutated so the module that did `from config import SETTINGS` keeps
the old one, and the cleanup code grows until it is its own source of
bugs. Each shape below replaces one of those hand-rolled patterns with
a binding whose removal is the library's problem.

## An environment variable: item=

`os.environ` is a mapping, so one entry of it is a value binding with
`item=`. `overrides()` holds the value while applied and restores the
prior state on exit, whether the variable existed before or not:

```python
>>> api_key = wrapture.binding(os.environ, item="API_KEY")

>>> with api_key.overrides("sk_test"):
...     price(100)
'[USD within 30.0s] total=120.00'

>>> "API_KEY" in os.environ
False

```

The other direction is `hides()`: the entry is absent while applied,
which is how the missing-configuration branch gets tested even on a
machine where the variable is set:

```python
>>> with api_key.overrides("sk_test"):
...     with wrapture.binding(os.environ, item="API_KEY").hides():
...         price(100)
Traceback (most recent call last):
    ...
RuntimeError: API_KEY is not configured

```

## The settings dict: mode="mapping"

`SETTINGS` is shared by reference: other modules did `from config
import SETTINGS` at import time, so replacing the dict on the module
would strand them with the old one. A mapping binding mutates the one
dict in place and never replaces it, so every holder sees the test's
content, and the original entries come back on exit in their original
order:

```python
>>> SETTINGS = config.SETTINGS      # a holder, as another module would have

>>> settings = wrapture.binding(config, "SETTINGS", mode="mapping")

>>> with settings.updates({"tax_rate": 0.0}), api_key.overrides("sk_test"):
...     price(100)
'[USD within 30.0s] total=100.00'

>>> with settings.overrides({"currency": "EUR", "tax_rate": 0.1}), api_key.overrides("sk_test"):
...     price(100)
'[EUR within 30.0s] total=110.00'

>>> SETTINGS == {"currency": "USD", "tax_rate": 0.2}, SETTINGS is config.SETTINGS
(True, True)

```

For readers coming from `unittest.mock`: `updates()` merges the named
keys over what is there, `patch.dict`'s default, and `overrides()`
makes the given entries the whole content, `patch.dict(...,
clear=True)`. Both took effect through the holder's reference and both
restored it.

## The module constant: attr=, then interception

The timeout is a module attribute. When all the test wants is a
different value in place, that is a value binding with `attr=`. A
module is named by its import path, so the test needs no import of its
own; the module object works in the same position when it is already
in hand, as `config` is everywhere above:

```python
>>> with wrapture.binding("config", attr="TIMEOUT").overrides(0.5), api_key.overrides("sk_test"):
...     price(100)
'[USD within 0.5s] total=120.00'

>>> config.TIMEOUT
30.0

```

The value binding holds a value; it observes nothing. When the question
becomes "does the retry path re-read the timeout, or did it cache it?",
name the same attribute positionally instead and the binding intercepts
the module attribute: each read becomes an event, and `on_get` can
shape what the reads see:

```python
>>> timeout = wrapture.binding("config", "TIMEOUT")
>>> _ = timeout.on_get.returns(0.5)

>>> with timeout, wrapture.timeline() as tape, api_key.overrides("sk_test"):
...     price(100)
...     price(100)
'[USD within 0.5s] total=120.00'
'[USD within 0.5s] total=120.00'

>>> [event.kind for event in tape.for_binding(timeout)]
['get', 'get']

```

Two calls, two reads: `price()` reads the timeout every time rather
than caching it, and the tape proves it. Under the covers the
interception form gives the module a private subclass of its type
while applied; the [module attributes](monkey-patching.md#attribute-bindings)
section covers the mechanics and the visible consequences.

## The formatter registry: item= with mode="callable"

The formatter is configuration too, a callable in a dict. A value
binding could swap the entry wholesale, but naming the entry with
`mode="callable"` wraps it instead: the stand-in is installed in the
slot, records like any bound callable, and the original entry comes
back on removal:

```python
>>> loud = wrapture.binding(config.FORMATTERS, item="plain", mode="callable")
>>> _ = loud.on_call.transforms_result(str.upper)

>>> with loud, api_key.overrides("sk_test"):
...     price(100)
'[USD within 30.0s] TOTAL=120.00'

>>> config.FORMATTERS["plain"](120.0)
'total=120.00'

```

The real formatter ran, its result was adjusted on the way out, and the
registry holds the original again after the block. Everything the call
vocabulary offers applies here, `returns()`, `raises()`, phases,
recording, because the entry is a callable binding like any other, just
addressed through a mapping slot instead of an attribute.

## One test, every shape at once

Bindings group, and a group applies and removes atomically, so a test
that needs all four shapes pins them in one declaration:

```python
>>> pinned = wrapture.bindings(
...     api_key=wrapture.binding(os.environ, item="API_KEY").overrides("sk_test"),
...     settings=wrapture.binding(config, "SETTINGS", mode="mapping").overrides(
...         {"currency": "EUR", "tax_rate": 0.0}
...     ),
...     timeout=wrapture.binding("config", attr="TIMEOUT").overrides(0.5),
... )

>>> with pinned:
...     price(100)
'[EUR within 0.5s] total=100.00'

>>> price(100)
Traceback (most recent call last):
    ...
RuntimeError: API_KEY is not configured

```

As a pytest fixture the group is a `with` around a `yield`, and the
plugin's leak sweep fails, by name, any test whose configuration
binding was left applied, the failure mode hand-rolled cleanup gets
wrong silently.

```python
import pytest
import wrapture

pytest_plugins = ["wrapture.pytest_plugin"]


@pytest.fixture
def pinned_config():
    group = wrapture.bindings(
        api_key=wrapture.binding(os.environ, item="API_KEY").overrides("sk_test"),
        settings=wrapture.binding(config, "SETTINGS", mode="mapping").overrides(
            {"currency": "EUR", "tax_rate": 0.0}
        ),
    )

    with group:
        yield group


def test_pricing_in_the_pinned_world(pinned_config):
    assert price(100) == "[EUR within 30.0s] total=100.00"
```

A pin only one test wants need not earn a fixture: the decorator form
holds a value binding around a single test, the direct counterpart of
`unittest.mock`'s `@patch.dict` as a decorator, with the binding
injected in case the test wants `hides()` or a different value
mid-flight:

```python
@wrapture.bound(os.environ, item="API_KEY").overrides("sk_test")
@wrapture.bound("config", attr="TIMEOUT").overrides(0.5)
def test_pricing_against_a_slow_gateway(API_KEY, TIMEOUT):
    assert price(100) == "[USD within 0.5s] total=120.00"
```

[Scoping with decorators](unit-testing.md#scoping-with-decorators)
covers the form.

## Where next

[Value bindings](monkey-patching.md#value-bindings-holding-a-value-in-place)
covers the full slot vocabulary, including instance attributes, a
stand-in module in `sys.modules`, and when to prefer `on_get` over
holding a value.
[Mapping bindings](monkey-patching.md#mapping-bindings-substituting-a-mappings-content)
has the exact semantics of `overrides()`, `updates()` and restoration.
[Module attributes](monkey-patching.md#attribute-bindings) explains the
class-swap mechanics behind the interception form and what it can and
cannot see. The [comparison page](coming-from-mock.md) maps
`unittest.mock`'s `patch.dict` and each `monkeypatch` helper onto
these shapes.
