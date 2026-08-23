# Watching a service over time

A service has been running in staging for a day. Nothing is on fire,
but nobody can say with confidence what it actually does: which
functions the traffic reaches, how often, how slow each one is, and
whether any of that changes through the day. The code has no
instrumentation, and adding some means an edit and a redeploy for
every question, with the answers scattered through a log file that
was never designed to be summed.

Recording every call for a day is the wrong tool, because the answer
is a handful of numbers per function, not a million events. What is
wanted is a summary that resets on a schedule, so the same table can
be compared hour against hour, a way to ask for one right now when
something looks off, and, for the occasional deep dive, a raw stream
that rotates rather than grows.

wrapture's windows and collectors are that shape. An `Aggregate`
keeps one row per bound location in bounded memory; a `Window` opens
runs on a schedule or on an operator's signal and turns each run into
a report; a `JSONLines` sink streams the raw events to rotating
files; and a config file makes the whole arrangement zero-code, so
the application under observation is never edited.

This example uses:
[collectors](scheduled-tracing.md#built-in-collectors-counter-and-aggregate),
[`window()` and `Window`](scheduled-tracing.md#in-code-window-and-window),
[triggers](scheduled-tracing.md#triggers-when-a-window-opens),
[reports](scheduled-tracing.md#reports-what-a-run-produces-and-where-it-goes),
[JSONLines](ad-hoc-tracing.md#streaming-to-disk-jsonlines),
[output paths and rotation](ad-hoc-tracing.md#output-paths-and-rotation),
[`shutdown()`](ad-hoc-tracing.md#process-and-scoped-listening) and
[`[[window]]` in config](scheduled-tracing.md#in-config-window).

## The service: a shop answering quotes

A stand-in small enough to fit here: a shop that answers price
quotes, one class per layer, and a driver that plays the part of a
day's traffic. One item in five is not in the catalog, so a share of
the calls raise, and the shop renders each answer as a JSON page
listing the whole catalog, which is where most of its own time goes.

```python
>>> import json
>>> import wrapture

>>> def price_list() -> dict[str, int]:
...     base = {"widget": 25, "gadget": 120, "gizmo": 60}
...     packs = {f"{name}-x{n}": price * n for name, price in base.items() for n in range(2, 21)}
...
...     return {**base, **packs}

>>> class Catalog:
...     PRICES = price_list()
...
...     def lookup(self, item: str) -> int:
...         return self.PRICES[item]

>>> class Pricing:
...     def __init__(self) -> None:
...         self.catalog = Catalog()
...
...     def quote(self, item: str, quantity: int = 1) -> dict[str, int | str]:
...         price = self.catalog.lookup(item)
...
...         return {"item": item, "quantity": quantity, "total": price * quantity}

>>> class Shop:
...     def __init__(self) -> None:
...         self.pricing = Pricing()
...
...     def handle(self, item: str) -> str:
...         quote = self.pricing.quote(item, quantity=2)
...         listing = [
...             {"item": name, "price": price}
...             for name, price in sorted(Catalog.PRICES.items())
...         ]
...
...         return json.dumps({"quote": quote, "catalog": listing}, indent=2)

>>> def traffic(shop: Shop, requests: int = 50) -> None:
...     items = ["widget", "gadget", "gizmo", "widget", "missing"]
...     for index in range(requests):
...         try:
...             shop.handle(items[index % len(items)])
...         except KeyError:
...             pass

```

Nothing here knows it will be observed. The bindings are declared
once and applied for the rest of the page; applied but with nothing
listening, they record nothing:

```python
>>> shop = Shop()
>>> service = wrapture.bindings(
...     handle=wrapture.binding(Shop, "handle"),
...     quote=wrapture.binding(Pricing, "quote"),
...     lookup=wrapture.binding(Catalog, "lookup"),
... )
>>> service.apply()
<BindingGroup ['handle', 'quote', 'lookup']>
>>> service.handle
<Binding 'Shop.handle' callable active>

```

One warm-up run before measuring anything, as with any profiling: the
first call through a code path pays for imports and caches that
later calls do not, and that one-off cost should not land in the
numbers.

```python
>>> traffic(shop)

```

## The naive approach: a log line and a stopwatch

The usual answer is a `time.perf_counter()` pair and a `log.info()`
in each function of interest, then a script over the log file to
group and average. Every new question is another edit and redeploy,
and the timings mislead: a function that is slow only because of what
it calls looks as guilty as one slow in its own right, because a
stopwatch around a call cannot see inside it.

## One report: the Aggregate collector in a window

`Aggregate` keeps one row per path and nothing else: how many calls
began and completed, how many raised, and total, self, fastest and
slowest times. It is a collector, so it lives in a window: the run
opens on entry, accumulates, and closes on exit with a report per
collector.

```python
>>> with wrapture.window(collect=[wrapture.Aggregate()]) as run:
...     traffic(shop, 2000)

>>> run
<Run 1 of 'window', closed>
>>> report = run.reports[0]
>>> print(report.text)  # doctest: +NORMALIZE_WHITESPACE
aggregate "aggregate" run 1, ... to ... (...s), pid ...
3 paths, 6,000 operations begun, 6,000 completed, 1,200 raised
<BLANKLINE>
calls  total   self  per-call    min    max  errors  path
2,000    ...    ...       ...    ...    ...     400  __main__:Shop.handle
2,000    ...    ...       ...    ...    ...     400  __main__:Pricing.quote
2,000    ...    ...       ...    ...    ...     400  __main__:Catalog.lookup
<BLANKLINE>

```

The header says which run this was, when it opened and closed in
local time, and whose process; the table has one row per bound
location, sorted by `self` time, the figure that answers "where is
the time going": `Shop.handle` spends more time in its own body than
`Pricing.quote` does, even though every quote runs inside a handle,
because self time excludes what the observed children account for.
The `errors` column appears only when something failed, and counts
an exception the code caught and noted with `note_exception()` the
same as one that escaped. The same figures are on the report as
data, for a dashboard or a test to read without parsing the table:

```python
>>> report.kind, report.window, report.run, report.cut_short
('aggregate', 'window', 1, False)
>>> report.data["begun"], report.data["raised"]
(6000, 1200)
>>> report.data["paths"]["__main__:Shop.handle"]["errors"]
400

```

`window()` suits a script or a one-off run of the traffic. For a
service that runs all day the schedule has to come from somewhere
else.

## Reports on a schedule: `Window` with `every=`

`Window` takes the same contents plus the triggers, and `start()`
arms it. `every=` on its own is the back-to-back shape: each run
lasts the whole period and closes as the next opens, so the report at
each boundary covers exactly one period, totals reset. Reports are
retained on the window (the last ten by default), and `on_report=`
hands each one to a callable the moment its run closes. Durations
are the forms `rotate=` accepts (`"30s"`, `"15m"`, `"1h"`, or a
number of seconds); an hour is too long to wait for here, so a tenth
of a second stands in, and the page waits for the first report to
arrive before sending the second burst of traffic:

```python
>>> import time
>>> seen = []
>>> ticking = wrapture.Window(
...     name="ticking", every=0.1, collect=[wrapture.Aggregate()], on_report=seen.append
... )
>>> ticking.start()
>>> traffic(shop)
>>> deadline = time.monotonic() + 5
>>> while not seen and time.monotonic() < deadline:
...     time.sleep(0.01)
>>> traffic(shop)
>>> ticking.stop()

>>> seen[0].run, seen[0].cut_short, seen[0].data["begun"]
(1, False, 150)
>>> ticking.reports[-1].cut_short
True
>>> ticking.runs >= 2
True

```

Run 1 closed on schedule and reached `seen` from the scheduler
thread; the run open when `stop()` was called was closed early and
its report says so with `cut_short`. Adding `duration=` (config key
`for`) gives the sampled shape, thirty seconds of every hour, say, so
the service pays nothing in between; `align=True` puts the openings
on the wall-clock hour.

## Reports as files: `report=` and its path template

An `on_report` callback needs code to receive it. The file form
needs none, and is the one a config file can spell: `report=` is an
output path template written once per run per collector, whole file
then rename, so a reader never sees a half-written report. Inside a
window three variables join the usual `{date}` and `{pid}`:
`{window}` is the window's name, `{first}` is when the schedule's
first run opened, the same for every run of one schedule, and `{run}`
is the run number. Driving the runs by hand shows the files appear:

```python
>>> import os
>>> import tempfile

>>> outputs = tempfile.TemporaryDirectory()
>>> daily = wrapture.Window(
...     name="daily",
...     every="1h",
...     collect=[wrapture.Aggregate()],
...     report=os.path.join(outputs.name, "{window}-{first}/run-{run:02}.txt"),
... )
>>> daily.start()
>>> traffic(shop)
>>> daily.close()
<Run 1 of 'daily', closed>
>>> daily.open()
True
>>> traffic(shop, 5)
>>> daily.stop()

>>> from pathlib import Path
>>> for path in sorted(Path(outputs.name).rglob("run-*.txt")):
...     print(path.relative_to(outputs.name).as_posix())
daily-...T...-...-.../run-01.txt
daily-...T...-...-.../run-02.txt

```

`start()` opened run 1 at once (no `after=`), `close()` ended it and
wrote its file, `open()` began run 2 by hand, and `stop()` closed
that one, cut short. On a real schedule the runs open and close by
themselves; the directory is one per schedule, and a restart starts a
new one, named by its own first run.

## Opening a window on demand: `on_signal` and `on_file`

Something looks odd at 15:40 and the hourly report is twenty minutes
away. `on_signal=` and `on_file=` open a run from outside the
process, no restart and no code: `kill -USR1 <pid>`, or `touch` a
path the window watches. Either can be the only trigger, or sit
beside `every=`, where kicks add runs to the schedule.

```python
peek = wrapture.Window(
    name="peek",
    on_signal="SIGUSR1",
    duration="60s",
    collect=[wrapture.Aggregate()],
    report="reports/{window}-{datetime}.txt",
)
peek.describe()   # 'on SIGUSR1, for 60s'
```

```python
>>> profile = wrapture.Window(
...     name="profile",
...     on_file=os.path.join(outputs.name, "profile-now"),
...     collect=[wrapture.Aggregate()],
... )
>>> profile.describe()
'on touch of ...profile-now, until the next kick'

```

`peek` collects for a minute after each signal, and a signal during
that minute is refused rather than overlapping, since runs never do.
`profile` has no `duration`, so a kick toggles: touch to start
collecting, touch again to report and start over, the file being
removed as it is consumed. The signal handler is installed by
`start()`, on the main thread, and neither window is started here.
`SIGUSR1` and `SIGUSR2` do not exist on Windows, where naming one is
refused at construction; `on_file` works everywhere, including under
servers that own the signal handlers.

## The always-on stream: `JSONLines` with `rotate=`

Sometimes the individual events are wanted after all, for a `jq`
query over the afternoon's failures. A `JSONLines` sink registered at
the process tier hears every event from every thread and writes it
as one JSON object per line, never blocking the observed call; a
time variable in the path plus `rotate=` keeps one file per day
rather than one forever:

```python
>>> stream = wrapture.add_sink(
...     wrapture.JSONLines(
...         os.path.join(outputs.name, "traces/trace-{date}.jsonl"), rotate="1d", align=True
...     )
... )
>>> traffic(shop, 5)

```

Lines are queued to a writer thread, so the file is complete only
once the sink is flushed. At interpreter exit wrapture flushes every
process sink and closes any open window run itself; `wrapture.shutdown()`
does exactly that on demand, for hosts that tear the interpreter down
without running atexit callbacks. Under mod_wsgi, subscribe it to the
process shutdown event, and everything owed is delivered while the
interpreter and its threads are still alive. Calling it more than
once is safe, and it uninstalls nothing.

```python
>>> wrapture.shutdown()
>>> os.path.basename(stream.path)
'trace-...-...-....jsonl'
>>> with open(stream.path) as lines:
...     events = [json.loads(line) for line in lines]
>>> len(events)
15
>>> sorted({event["path"].rpartition(":")[2] for event in events})
['Catalog.lookup', 'Pricing.quote', 'Shop.handle']
>>> [event["exception"]["type"] for event in events if "exception" in event]
['KeyError', 'KeyError', 'KeyError']

```

Releasing everything the page applied:

```python
>>> wrapture.remove_sink(stream)
>>> stream.close()
>>> service.remove()
<BindingGroup ['handle', 'quote', 'lookup']>
>>> service.handle
<Binding 'Shop.handle' callable unapplied>
>>> outputs.cleanup()

```

## The same thing from a config file: the Flask shop

The `examples/flask-app` directory in the repository is a real
version of this scenario: a small Flask shop in `myapp.py` that never
imports wrapture, and a `wrapture.toml` whose `[[instrument]]` entry
instruments Flask itself, so every request records as one tree with
its view function beneath it, plus an `[[observe]]` entry for the
app's own `quote` helper. Its shipped config ends in a printer sink,
the live view; for a day in staging, replace that with a rotating
stream and a scheduled report:

```toml
pythonpath = "."

[[observe]]
target = "myapp"
name = "quote"

[[instrument]]
name = "wrapture_local.flask_support:FlaskInstrumentation"

# The raw stream, one file per day, rotated at local midnight.
[[sink]]
type = "jsonlines"
path = "traces/trace-{date}.jsonl"
rotate = "1d"
align = true

# A summary of the app's own functions every minute, on the minute,
# one file per run, grouped by the schedule's start.
[[window]]
name = "minutely"
every = "1m"
align = true
report = "reports/{window}-{first}/run-{run:02}.txt"

[[window.collect]]
type = "aggregate"
filter = { kind = "call" }

# On demand: kick with `kill -USR1 <pid>` for a minute's summary.
[[window]]
name = "kick"
on_signal = "SIGUSR1"
for = "60s"
report = "reports/{window}-{datetime}.txt"

[[window.collect]]
type = "aggregate"
filter = { kind = "call" }
```

`filter` on a collect entry is the same gating key a sink takes; here
it keeps the request events out of the table, so the report is about
the view functions and helpers and the request stream is left to the
JSONLines file. Relative paths anchor to the config file's directory.
`every = "1m"` is for watching reports appear; for a day in staging
`every = "1h"` gives one file per hour with the totals reset.

Run the app under the runner, so the config is applied before Flask
is imported, and put load on it with any HTTP load tool, here
[bombardier](https://github.com/codesenberg/bombardier):

```console
$ cd examples/flask-app
$ uv run --with flask python -m wrapture --config wrapture.toml -m flask --app myapp run --port 8000
```

```console
$ bombardier -c 4 -d 3m http://localhost:8000/quote/widget
$ bombardier -c 4 -d 3m http://localhost:8000/export
$ ls reports/minutely-*/
run-01.txt  run-02.txt  run-03.txt
```

Each file is one minute's table, and the picture emerges from
comparing them: which views the traffic reaches, how many quotes
fail, and whether `per-call` moves as the load does. A whole-process
run of the same config over a few thousand requests through the
Flask test client produced this:

```text
aggregate "aggregate" run 1, 2026-08-18 18:25:42 to 18:25:43 +10:00 (1.1s), pid 16745
4 paths, 2,400 operations begun, 2,400 completed, 600 raised

calls   total    self  per-call   min    max  errors  path
  900  74.7ms  71.7ms      83us  57us  415us     300  myapp:quoted
  300   9.1ms   9.1ms      30us  28us   97us          myapp:index
  300   4.0ms   4.0ms      13us  12us   48us          myapp:export
  900   2.9ms   2.9ms       3us   1us   13us     300  myapp:quote
```

When something looks off between reports, `kill -USR1 <pid>` opens
the `kick` window for a minute and drops a timestamped report beside
the scheduled ones, no restart involved. Under a pre-fork server each
worker has its own windows, so put `{pid}` in the report path and
add `jitter` to spread the aligned openings. The raw stream is there
for when a summary raises a question only the events can answer:

```console
$ jq -c 'select(.exception)' traces/trace-2026-08-18.jsonl | head
```

## Where next

The [scheduled tracing page](scheduled-tracing.md) is the full
reference for windows: every trigger, the report shape, clocks and
restarts, and how to write a collector of your own. The
[ad-hoc tracing page](ad-hoc-tracing.md) covers sinks, output paths
and rotation, the config file grammar and the runner, and the
[WSGI request tracing page](wsgi-tracing.md) explains what the
request events in the stream contain and how the Flask
instrumentation records them.
