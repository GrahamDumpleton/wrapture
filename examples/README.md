# wrapture examples

Small, runnable demonstrations of zero-code tracing with the
`python -m wrapture` runner and the `python -m wrapture.tools`
commands. Each subdirectory is self-contained: the code being
observed, an entry script, and the `wrapture.toml` the runner picks
up from the working directory.

In every example the observed code never imports wrapture. The entry
script imports it from a sibling module, and the runner applies the
config before the script runs, so the patches are in place before
that import happens. That split matters when writing your own: the
runner executes the entry script under the name `__main__`, so
members to observe must live in an importable module, not in the
entry script itself.

## Running

From a checkout of this repository, uv runs everything against the
project environment. Change into an example directory first, since
the runner finds `wrapture.toml` in the current directory:

```console
$ cd examples/live-printer
$ uv run python -m wrapture main.py
```

With wrapture installed into an environment of your own, drop the
`uv run` prefix and use plain `python -m wrapture main.py` anywhere
below.

## Running through autowrapt instead

Every example also runs through the injection path, where no
launcher is involved: autowrapt fires wrapture's bootstrap at
interpreter startup, the same config is discovered from the working
directory, and the program is started with plain `python`. From the
checkout, uv supplies autowrapt for just that run:

```console
$ cd examples/live-printer
$ AUTOWRAPT_BOOTSTRAP=wrapture uv run --with autowrapt python main.py
```

With wrapture and autowrapt both installed in an environment of your
own, the same is:

```console
$ AUTOWRAPT_BOOTSTRAP=wrapture python main.py
```

The output is identical to the runner's, because the runner and
autowrapt are two triggers for the same config machinery. The config
applies during interpreter startup, but observe entries defer:
bindings land when the application itself imports the observed
module, so nothing has to be importable that early. Only
operator-code carries a `pythonpath` entry, because its sink factory
resolves when the config loads and lives in an uninstalled package
next to it.

## live-printer

The quickest look at what observing feels like. A shop places three
orders, one of which the payment gateway declines. The config binds
the order flow (`name` for exact members, `match` with an exclude for
the gateway) to a `printer` sink, so the call tree prints live to
stderr while the orders run: one line as each call begins, indented
by nesting, `->` lines for results and a `!!` line where the
declined card raises.

```console
$ cd examples/live-printer
$ uv run python -m wrapture main.py
```

## stream-to-disk

A pipeline processes four sources on two worker threads while a
`jsonlines` sink streams every completed event to `trace.jsonl`. A
config sink is a process sink, so it hears all the threads with no
timeline anywhere. The sink appends across runs, so delete the file
first for a clean trace:

```console
$ cd examples/stream-to-disk
$ rm -f trace.jsonl
$ uv run python -m wrapture main.py
```

Then render the trace. For a timeline, convert to Chrome trace JSON
and drop the result onto <https://ui.perfetto.dev>: one lane per
worker thread, nested slices per call, and clicking a slice shows
the captured arguments and result:

```console
$ uv run python -m wrapture.tools convert --format chrome -o trace.json trace.jsonl
```

The other formats write to standard output, for pasting into a
GitHub comment or comparing as a snapshot:

```console
$ uv run python -m wrapture.tools convert --format mermaid trace.jsonl
$ uv run python -m wrapture.tools convert --format canonical trace.jsonl
```

## operator-code

The config extensibility story in one directory: everything beyond
plain observation lives in an ordinary Python package next to the
config file, reached only by the references in `wrapture.toml`.

- `pythonpath = "."` is anchored to the config file's directory, so
  the `wrapture_local` package beside it is importable however the
  process was launched.
- The `[[setup]]` entry names a callback that runs when the `shop`
  module is imported; it binds the gateway with a `when=` predicate,
  and the entry's extra `threshold` key reaches the callback as a
  keyword argument, so of the six charges made only the two over 400
  are recorded, with the cutoff adjustable in the config file alone.
- The `[sink]` type names a factory that composes a `Fanout` of a
  live printer and a JSONLines file, a combination the TOML sink
  table alone cannot spell.

```console
$ cd examples/operator-code
$ rm -f trace.jsonl
$ uv run python -m wrapture main.py
```

Two charges print live and land in `trace.jsonl`; convert the file
as in the previous example to see the same two from the other
renderings. Edit `threshold` in `wrapture.toml` and run again to
watch the selection change without touching any Python.
