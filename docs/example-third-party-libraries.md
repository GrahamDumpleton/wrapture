# Changing what a third-party library does

A vendored HTTP client sends every request the application makes. It
has no hook for what you now need: every request must carry a tenant
header, a transient connection reset should be retried once, and in
development the timeout it reads from a class attribute must be
clamped. The library is not yours to edit, and forking it to add three
lines means carrying that fork forever.

Assigning replacement functions onto the module is the traditional
answer, and it works until you need it not to: a hand-rolled patch has
no off switch, leaves no record of what was changed, cannot be
reconfigured without being reinstalled, and has to be placed after the
library's import in a way nothing enforces. wrapture treats a patch as
an object with a lifecycle: it wraps the real method rather than
replacing it, applies and removes cleanly, can be suspended and
reconfigured while installed, reports its own state honestly, and can be
installed at process start from a config file without the application
knowing.

This example uses: [call behaviour](monkey-patching.md#call-behaviour-changing-what-a-call-does),
[applying and removing](monkey-patching.md#applying-and-removing),
[suspending and resuming](monkey-patching.md#suspending-and-resuming),
[attribute bindings](monkey-patching.md#attribute-bindings),
[capture policies](unit-testing.md#recording-calls-on-a-timeline),
[the wrapt escape hatch](monkey-patching.md#escape-hatch-dropping-down-to-wrapt)
and [configuring from a file](ad-hoc-tracing.md#configuring-from-a-file).

## The library you cannot edit

The client is a stand-in for the vendored library. Its shape is the
usual one: a `request()` method that assembles headers, reads a
timeout from a class attribute, and hands the call to a transport. The
transport here just echoes what it was given, so every example below
shows exactly what the library would have sent:

```python
>>> import sys
>>> import wrapture

>>> class Client:
...     timeout: int = 30
...
...     def __init__(self, base_url: str, transport) -> None:
...         self.base_url = base_url
...         self.transport = transport
...
...     def request(self, method: str, path: str, headers=None, token=None) -> dict:
...         headers = dict(headers or {})
...         if token is not None:
...             headers["Authorization"] = f"Bearer {token}"
...
...         return self.transport(method, self.base_url + path, headers, self.timeout)

>>> def echo(method: str, url: str, headers: dict, timeout: int) -> dict:
...     return {"method": method, "url": url, "headers": headers, "timeout": timeout}

>>> client = Client("https://api.example", echo)
>>> client.request("GET", "/orders")
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {}, 'timeout': 30}

```

## The naive approach: assign over the method

The direct route is to save the original and assign a replacement:

```python
_original = Client.request

def request(self, method, path, headers=None, token=None):
    headers = {**(headers or {}), "X-Tenant": "acme"}
    return _original(self, method, path, headers=headers, token=token)

Client.request = request
```

This has to re-declare the method's signature, and silently breaks when
the library changes it. Nothing records that `Client.request` was
patched, so a debugger shows a plain function with a different
`__code__` and no explanation. Turning it off means assigning
`_original` back, which is wrong if anyone else patched the same
method in between. And there is no way to say "keep the patch, but stop
it for a moment".

## Injecting the header: a transform on the way in

A binding names the method and holds behaviour for it. `transforms_args`
receives the call as the caller made it, `(args, kwargs)`, and returns
what the real method should receive. The real `request()` still runs;
only the `headers` keyword is different by the time it does:

```python
>>> def with_tenant(args, kwargs):
...     headers = {**(kwargs.get("headers") or {}), "X-Tenant": "acme"}
...     return args, {**kwargs, "headers": headers}

>>> request = wrapture.binding(Client, "request")
>>> request.on_call.transforms_args(with_tenant)
<CallBehaviour of <Binding 'Client.request' callable unapplied>>

```

Nothing has changed yet: the repr says `unapplied`, and creating and
configuring a binding never patches anything. `apply()` installs the
wrapper, and every call through the class picks up the header, merged
with whatever the caller passed:

```python
>>> request.apply()
<Binding 'Client.request' callable active>
>>> client.request("GET", "/orders")
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {'X-Tenant': 'acme'}, 'timeout': 30}
>>> client.request("GET", "/orders", headers={"Accept": "text/csv"})
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {'Accept': 'text/csv', 'X-Tenant': 'acme'}, 'timeout': 30}

```

The transform sees the arguments as written, so a caller passing
`headers` positionally would need handling too; for a signature that
callers use in many spellings, `decorates()` below gives you the
whole call to normalise as you see fit.

## Reversibility: suspend, resume, remove

The patch stays visible as an object whose state can be queried at any
time. `applied` says whether you installed it and `active` inspects the
target on every access, so if some other code replaced `Client.request`
wholesale the binding would say so:

```python
>>> request.applied, request.active, request.suspended
(True, True, False)

```

`suspend()` makes the wrapper inert without removing it. This is the
safe way to switch a patch off in a live process: the wrapper keeps its
place in the chain, so it can be toggled while other parties have
wrapped the same method, and calls that pass through while suspended
are counted:

```python
>>> request.suspend()
<Binding 'Client.request' callable active suspended>
>>> client.request("GET", "/orders")
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {}, 'timeout': 30}
>>> request.suspended_calls
1
>>> request.resume()
<Binding 'Client.request' callable active>

```

`remove()` uninstalls the wrapper and restores the original exactly, and
a removed binding can be applied again later; for a patch scoped to a
block, the binding is a context manager. Both appear further down.

## Reconfiguring the live patch

Behaviour can be changed while the wrapper is installed, without
removing and re-applying it. Composing stages such as `transforms_args`
accumulate in the order added, so switching the tenant means dropping
the current pipeline with `passes_through()` and setting the new one;
the patch itself never leaves the method:

```python
>>> def with_other_tenant(args, kwargs):
...     headers = {**(kwargs.get("headers") or {}), "X-Tenant": "globex"}
...     return args, {**kwargs, "headers": headers}

>>> request.on_call.passes_through().transforms_args(with_other_tenant)
<CallBehaviour of <Binding 'Client.request' callable active>>
>>> client.request("GET", "/orders")
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {'X-Tenant': 'globex'}, 'timeout': 30}

```

If calls may be in flight on other threads while you reconfigure,
`suspend()` first, reconfigure, then `resume()`, so no call sees a
half-built pipeline.

## Retrying with a wrapper around the whole call

Adding a retry needs control of the whole call: run it, catch one kind
of failure, run it again. `decorates()` takes a function with wrapt's
wrapper signature and makes it the centre of the pipeline; the tenant
transform already configured still wraps around it, because
`transforms_*` stages compose while `decorates()` replaces only the
terminal stage. A transport that drops its first connection shows the
retry working:

```python
>>> def retry_once(wrapped, instance, args, kwargs):
...     try:
...         return wrapped(*args, **kwargs)
...     except ConnectionError:
...         return wrapped(*args, **kwargs)

>>> request.on_call.decorates(retry_once)
<CallBehaviour of <Binding 'Client.request' callable active>>

>>> class DropsFirst:
...     def __init__(self) -> None:
...         self.calls = 0
...
...     def __call__(self, method: str, url: str, headers: dict, timeout: int) -> dict:
...         self.calls += 1
...         if self.calls == 1:
...             raise ConnectionError("connection reset")
...
...         return echo(method, url, headers, timeout)

>>> flaky = Client("https://api.example", DropsFirst())
>>> flaky.request("GET", "/orders")
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {'X-Tenant': 'globex'}, 'timeout': 30}
>>> flaky.transport.calls
2

```

The same function could carry a `@wrapt.decorator` in production code;
`decorates()` takes it undecorated. Now take the patch down. The class
is exactly as it was, and the binding is back to `unapplied`, ready to
be applied again:

```python
>>> request.remove()
<Binding 'Client.request' callable unapplied>
>>> client.request("GET", "/orders")
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {}, 'timeout': 30}

```

## Clamping an attribute the library reads

`Client.request` reads `self.timeout`, a plain class attribute. There
is no method to wrap for that, but an attribute binding intercepts the
read itself: on `apply()` it installs a descriptor on the class over
the existing default, and `on_get.transforms` rewrites every value an
instance reads through it, whether the class default or an instance
override. Used as a context manager, it is a scoped clamp:

```python
>>> timeout = wrapture.binding(Client, "timeout")
>>> timeout
<Binding 'Client.timeout' attribute unapplied>
>>> timeout.on_get.transforms(lambda value: min(value, 5))
<GetBehaviour of <Binding 'Client.timeout' attribute unapplied>>

>>> with timeout:
...     client.timeout = 120
...     client.request("GET", "/orders")
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {}, 'timeout': 5}

>>> del client.timeout
>>> client.request("GET", "/orders")
{'method': 'GET', 'url': 'https://api.example/orders', 'headers': {}, 'timeout': 30}

```

The mode was detected from what was found at the target: `timeout` is
plain data, so the binding is in attribute mode and offers `on_get`,
`on_set` and `on_delete` instead of `on_call`. One limit shapes where
this applies: attribute bindings intercept access through instances,
so a library reading `Client.timeout` off the class would not be
affected (see [known limitations](known-limitations.md)). A
module-level constant is bound the same way, with the module as the
owner; see
[Attribute bindings](monkey-patching.md#attribute-bindings).

## Applying before the library is imported

A binding resolves its target when created, so the module must already
be imported. wrapt's deferred form, a trailing `?` on the module name
that registers a post-import hook instead, is refused, because a
binding must hold the wrapper it applied in order to remove, suspend
and report on it:

```python
>>> wrapture.binding("vendored_client?", "Client.request")
Traceback (most recent call last):
    ...
wrapture.exceptions.DeferredTargetError: deferred patching is not supported: ...

```

What is supported is running the binding code from a post-import
hook. `wrapture.when_imported` (wrapt's decorator of the same name,
re-exported so application code need not import wrapt for this one
job) registers a callback against a module name; the callback runs
with the module as its argument the moment that module is imported,
or immediately if it already was. To show it firing, the vendored
client here is written to disk as a real module the interpreter has
not yet seen:

```python
>>> import pathlib
>>> import tempfile

>>> libdir = tempfile.TemporaryDirectory()
>>> _ = pathlib.Path(libdir.name, "vendored_client.py").write_text(
...     "class Client:\n"
...     "    def request(self, method, path):\n"
...     "        return {'method': method, 'path': path, 'headers': {}}\n"
... )
>>> sys.path.insert(0, libdir.name)

>>> installed: list[wrapture.Binding] = []

>>> @wrapture.when_imported("vendored_client")
... def install(module):
...     request = wrapture.binding(module.Client, "request")
...     request.on_call.transforms_result(lambda r: {**r, "headers": {"X-Tenant": "acme"}})
...     installed.append(request.apply())

>>> "vendored_client" in sys.modules
False
>>> installed
[]

>>> import vendored_client
>>> installed
[<Binding 'Client.request' callable active>]
>>> vendored_client.Client().request("GET", "/orders")
{'method': 'GET', 'path': '/orders', 'headers': {'X-Tenant': 'acme'}}

```

The hook created the binding after the import it depends on, by
construction, and the patch was in place before any caller could
reach `request()`. Because it runs on the first import of that
module, wherever that import happens, the application only has to
register the hook early (its own package `__init__`, or a startup
module) and no longer cares which of its modules imports the client
first. Keeping the applied binding in a module-level list is
deliberate: the hook's local variable is gone once it returns, so
hold the handle if you want to `suspend()` or `remove()` the patch
later. Registering after the module is already imported is harmless,
the hook simply runs at once, so the same code works whether or not
something imported the library first.

The function form, `wrapture.register_post_import_hook(callback,
"module.name")`, does the same without the decorator, for the case
where the patches are built in a loop or from data.

```python
>>> _ = installed[0].remove()
>>> vendored_client.Client().request("GET", "/orders")
{'method': 'GET', 'path': '/orders', 'headers': {}}

```

The config layer offers the same trigger without any code in the
application. An `[[instrument]]` entry names an `Instrumentation`
class whose `apply()` runs with the module as its argument as soon as
the trigger module is imported, or immediately if it already was, and
the binding it creates lands before the application's first call.
Applied by `python -m wrapture` or by autowrapt, the config is in
place before the application imports anything, so the ordering nothing
enforced in the naive version is now guaranteed by the launcher. The
last section shows the file.

## Recording calls without recording the token

An installed patch is also an observation point, and the honest way to
check what a library is doing is to listen. `request()` takes a bearer
token, so record it with a capture policy that redacts that parameter
by name; the token also comes back out inside the `Authorization`
header of the echoed result, so capture the result as a type name only.
A `Printer` sink shows each call as it happens:

```python
>>> audited = wrapture.binding(
...     Client, "request",
...     capture_args=wrapture.redact("token"),
...     capture_result="types",
... )
>>> printer = wrapture.add_sink(wrapture.Printer(sys.stdout, timing=False))

>>> with audited:
...     _ = client.request("GET", "/orders", token="s3cr3t")
Client.request(method='GET', path='/orders', headers=None, token='<redacted>')
Client.request -> '<dict>'

>>> wrapture.remove_sink(printer)

```

Redaction matches by parameter name against the normalised call, so it
covers the token however the caller spelt the argument. Everything not
named is captured at the reference level unless `redact(..., level=)`
says otherwise. Behaviour and capture policy live on the same binding,
so the tenant transform and the redaction can be one object.

## Escape hatch: the wrapt handle underneath

Occasionally one call must bypass the patch entirely, or something must
inspect what wrapt actually installed. The binding exposes its
`wrapt.FunctionWrapper` as `wrapper` while applied, and the original
function is its `__wrapped__`, so a bypass is a direct call to that:

```python
>>> request = wrapture.binding(Client, "request")
>>> request.on_call.transforms_args(with_tenant).apply()
<Binding 'Client.request' callable active>

>>> request.wrapper.__wrapped__(client, "GET", "/health")
{'method': 'GET', 'url': 'https://api.example/health', 'headers': {}, 'timeout': 30}
>>> client.request("GET", "/health")
{'method': 'GET', 'url': 'https://api.example/health', 'headers': {'X-Tenant': 'acme'}, 'timeout': 30}

>>> request.remove()
<Binding 'Client.request' callable unapplied>

```

`request.target` and `request.name` are the patch coordinates, and
`wrapt.unwrap_object(request.target, request.name, request.wrapper)`
is exactly what `remove()` does. Anything core wrapt can do with those
three remains reachable, and if code outside wrapture removes the
wrapper behind your back, the binding's repr changes to `displaced`
rather than pretending.

## The same thing from a config file

Everything above ran in a process that already had the library
imported. To install the header patch at startup, without the
application mentioning wrapture, put the binding in an
`Instrumentation` class next to a `wrapture.toml`. The `pythonpath`
key makes the companion code importable, anchored to the config
file's own directory, and any key on the `[[instrument]]` entry other
than `name` is one of the settings the class declares:

```toml
pythonpath = "."

[[instrument]]
name = "wrapture_local.patches:ClientInstrumentation"
tenant = "acme"
```

```python
# wrapture_local/patches.py
import wrapture


class ClientInstrumentation(wrapture.Instrumentation):
    """Stamp the tenant header on every vendored client request."""

    target = "vendored_client"
    modules = ("vendored_client",)
    removable = True
    settings = {"tenant": wrapture.Setting("", "the tenant to send")}

    def apply(self, name, module):
        tenant = self.settings["tenant"]

        def with_tenant(args, kwargs):
            headers = {**(kwargs.get("headers") or {}), "X-Tenant": tenant}
            return args, {**kwargs, "headers": headers}

        request = wrapture.binding(module.Client, "request")
        request.on_call.transforms_args(with_tenant)
        request.apply()

        self.on_remove(request.remove)
```

`apply()` receives the freshly imported `vendored_client` module, so
`module.Client` is the real class, and the binding is created after
the import it depends on by construction. The module defining the
class imports only wrapture, so naming it from the config never
causes the library to be imported ahead of time. Registering the
binding's `remove()` with `on_remove()` is what puts the patch inside
what the config can undo: `AppliedConfig.revert()` calls the class's
`remove()` for every trigger it applied, most recent first, and the
`threshold`-style setting a typo would have silently dropped is
instead a `ConfigError` at load, because the declaration says which
keys exist.

Run the application through the launcher, or point the environment at
the file:

```console
$ python -m wrapture -m myapp
$ WRAPTURE_CONFIG=/etc/myapp/wrapture.toml python -m wrapture manage.py runserver
```

Without `--config`, `python -m wrapture` looks in `WRAPTURE_CONFIG`,
then `wrapture.toml` in the current directory, then a `[tool.wrapture]`
table in `pyproject.toml`, and finding none is an error. Where the
command line is not yours (a service manager, a container entry point),
[autowrapt](ad-hoc-tracing.md#injection-without-a-launcher-autowrapt)
applies the same file at interpreter startup, gated on the package
being installed and `AUTOWRAPT_BOOTSTRAP=wrapture` being set.

Loading a config runs the code it names, so the trust boundary is
write access to the file. Failures are loud: an `apply()` that raises
while the config is being applied propagates to the caller, and one
that raises later, from inside the application's own import of the
library, warns with `ConfigWarning` and lets the import continue,
because a patch must never fail the import it rode in on.

## Where next

[Call behaviour](monkey-patching.md#call-behaviour-changing-what-a-call-does)
lists the full vocabulary, including `validates_args()` and
`transforms_result()`, and explains how composing and terminal stages
form the pipeline. [Attribute bindings](monkey-patching.md#attribute-bindings)
covers `on_set` and `on_delete`, and decorating a bound method per
access. [Configuring from a file](ad-hoc-tracing.md#configuring-from-a-file)
has the whole file format, including `[[observe]]` entries for pure
observation and `[[instrument]]` entries naming packaged
instrumentation by its entry point name, and
[known limitations](known-limitations.md) lists what a binding cannot
intercept and why.
