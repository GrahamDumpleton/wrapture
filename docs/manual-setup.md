# Manual setup

A config file has three doorways into a process. The
`python -m wrapture` runner wraps the launch; autowrapt injects the
config at interpreter startup; and application code can apply the
same config itself, with a few lines placed deliberately in its own
startup. This page is the third doorway.

Sometimes manual setup is simply a preference: a CLI tool, worker or
daemon that treats observability as one of its own features and wants
the wiring explicit. Often it is structural: an embedded interpreter
or a multi-process manager makes wrapping startup from the outside
somewhere between awkward and impossible. mod_wsgi embeds Python
inside Apache, so there is no `python` command line to hand to the
runner; gunicorn is its own executable, forking workers whose
lifecycle it owns. In those environments a few lines at the top of
the right file are simpler than bending the process manager, and the
recipes below say which file that is.

## Apply before the application imports

The whole pattern is four lines, and their position matters more
than their content:

```python
import wrapture

source = wrapture.find_config()
if source is not None:
    wrapture.load_config(source).apply()

# Only now the application itself.

from myapp.web import main
```

`apply()` registers a post-import hook per target module, and a hook
fires immediately for a module already imported, so a late apply
usually still lands its bindings. What a late apply cannot fix is
history: calls that already ran went unobserved, and a reference
copied out before the patch stays unpatched, so a
`from myapp.orders import place_order` taken earlier holds the
original function forever. Applying first, in an entry module that
has imported nothing of the application yet, is the same ordering
guarantee the runner and autowrapt give, which is why setup belongs
as early in startup as it can possibly go.

`apply()` returns the `AppliedConfig`: its `pending` names observe
entries still waiting for their module, `report()` renders the whole
picture for a startup log, and `revert()` takes everything down
again if the host ever needs that.

## Choosing the source

`find_config()` implements the standard precedence: a path in the
`WRAPTURE_CONFIG` environment variable, then `wrapture.toml` in the
current directory, then a `[tool.wrapture]` table in
`pyproject.toml`, first found winning outright. Returning None when
nothing nominates a config makes the four-line pattern safe to leave
in permanently: a process with no config pays one existence check
and starts clean.

An application whose own options name the file calls
`load_config(path)` with that path instead. The catch is where
option parsing lives: if reaching the code that knows the path means
importing half the application first, the ordering guarantee is
already spent by the time the path is known. When startup cannot
learn the path early, let the environment carry it: an operator sets
`WRAPTURE_CONFIG`, and the entry module needs nothing imported but
`wrapture` itself before setup proceeds. To honour only the
environment, and not a `wrapture.toml` that happens to be lying in
the working directory, gate on the variable directly:

```python
import os

import wrapture

if "WRAPTURE_CONFIG" in os.environ:
    wrapture.load_config(os.environ["WRAPTURE_CONFIG"]).apply()
```

## A config without the file

`Config` is the programmatic primitive beneath the TOML file:
everything a file can say is expressible directly, applied and
reverted the same way. It suits the program whose own command line
options are the configuration, a tool that decides at runtime what
to observe and where events go, and never wants a file at all:

```python
import wrapture

config = wrapture.Config(
    observe=[
        wrapture.ObserveEntry(target="myapp.orders:OrderService", match="*"),
    ],
    sink=wrapture.JSONLines("trace.jsonl"),
)

applied = config.apply()
```

The all-in-code posture from [Ad-hoc tracing](ad-hoc-tracing.md) is
the other way to skip the file: `add_sink()`, bindings and
`instrumentation()` called directly, no Config object involved. The
difference is lifecycle: a `Config` applies and reverts as one unit
and defers per target module, where the direct calls take effect
each at its own moment. Either is fine; the Config form exists so
that "the file's semantics, without the file" is one object.

## Delivering output at exit

The first time anything registers output to deliver, wrapture
installs an `atexit` hook that calls `wrapture.shutdown()`: open
window runs close, writing their reports, and every process sink
flushes. A normal interpreter exit therefore needs nothing from you,
a short-lived CLI tool included.

`shutdown()` is also a public function, for environments where
atexit cannot be relied on: an embedded or sub interpreter may be
destroyed without its atexit callbacks ever running. It is safe to
call any number of times, and it quiesces without uninstalling:
bindings stay applied and configs are not reverted, so a host that
calls it early has flushed its output, not torn out its tracing. A
host that wants the tracing gone as well calls the applied config's
`revert()`.

## mod_wsgi

mod_wsgi embeds the interpreter inside Apache, so the runner is
unavailable and the WSGI script file is the entry module: mod_wsgi
imports it before the application, which makes its top the right
place for setup. The same file is where shutdown wants wiring,
because atexit is exactly what an embedded interpreter cannot
promise; mod_wsgi instead provides its own process shutdown
notification, delivered while the interpreter is still fully alive,
and subscribing that to `wrapture.shutdown()` restores the guarantee
that atexit gives an ordinary process:

```python
# myapp.wsgi

import wrapture

source = wrapture.find_config()
if source is not None:
    wrapture.load_config(source).apply()

try:
    import mod_wsgi
except ImportError:
    pass
else:
    def _flush(*args, **kwargs):
        wrapture.shutdown()

    mod_wsgi.subscribe_shutdown(_flush)

from myapp.web import application
```

The ImportError guard keeps the script honest under any other WSGI
server, where `mod_wsgi` does not exist as a module and plain atexit
already suffices. Each mod_wsgi daemon process runs the script once,
so each worker process applies its own config and flushes its own
sinks, with no cross-process coordination needed.

## gunicorn

gunicorn is its own executable, so the runner is out, but its
workers are ordinary Python processes, so atexit stands: a worker
stopped gracefully flushes on exit with no extra wiring. A worker
the master kills hard, the timeout path, delivers nothing buffered,
which no hook can fix; keep sinks that must not lose data unbuffered
or externally drained.

The simplest placement is the top of the WSGI module the command
line names, the `myapp/wsgi.py` behind `myapp.wsgi:application`,
exactly as in the mod_wsgi script above minus the mod_wsgi block.
With gunicorn's default lazy loading each worker imports that module
after the fork, so the bindings, the sinks and any threads they own
all belong to the worker, the posture easiest to reason about. With
`preload_app` the module imports once in the master before forking
and the workers inherit the setup; wrapture keeps recording coherent
across the fork, flushing sinks before it and reinitialising in the
child, as [Forked worker
processes](ad-hoc-tracing.md#forked-worker-processes) describes, so
this also works, with a `{pid}` path template giving each worker its
own output file.

The alternative placement is gunicorn's own config file hooks, which
suit applying per worker regardless of preload, plus an explicit
flush point on the way out:

```python
# gunicorn.conf.py

import wrapture


def post_fork(server, worker):
    source = wrapture.find_config()
    if source is not None:
        wrapture.load_config(source).apply()


def worker_exit(server, worker):
    wrapture.shutdown()
```

`post_fork` runs in the worker just after the fork and, without
preload, before the application module loads, preserving the
apply-before-import ordering. `worker_exit` runs in the worker as it
exits on the graceful paths; since `shutdown()` is idempotent, it
costs nothing that atexit would not already have done, and covers a
worker torn down in a way that skips atexit. Neither hook fires on a
SIGKILL, which is the caveat above, not a hook selection problem.

## The point of doing it manually

Injection from outside is the zero-code ideal, and where it works,
[the runner and autowrapt](ad-hoc-tracing.md#zero-code-runs-python--m-wrapture)
need nothing from this page. The environments that resist it share a
shape: something else owns process startup, multiplies processes, or
embeds the interpreter, and intercepting that from outside means
fighting the manager. Manual setup inverts the deal: four lines you
own, placed where you know they run first, in every process that
matters, with shutdown wired to whatever notification the host
actually honours. For multi-process and embedded deployments that
trade is usually the simpler one.
