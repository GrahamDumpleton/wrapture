# Scheduled tracing: windows and collectors

The sinks on the [ad-hoc tracing page](ad-hoc-tracing.md) listen for
as long as they are registered, which for a config sink is the life
of the process. A **window** is the other shape: a named span of time
during which its contents listen or collect, opened on a schedule or
on demand, closed after a duration or at the next opening, and ending
each run in a report. Outside a run the window's contents hear
nothing, so a binding whose only listener is a closed window costs
what an unobserved call costs.

Two things live inside a window. **Sinks**, the same ones as
anywhere else, become per-run streams: a printer or JSONLines file
in a window opens its file when a run opens and releases it when the
run closes, one file per run. **Collectors** accumulate while a run
is open and hand back a `Report` object when it closes; `Counter`
and `Aggregate` are the built-in ones, described below, and the
protocol is small enough to implement in a few lines.

## In code: `window()` and `Window`

`wrapture.window()` is the sibling of `timeline()`: the run opens on
entry, closes on exit, and is yielded so its reports can be read
afterwards.

```python
with wrapture.window(collect=[wrapture.Aggregate(), wrapture.Printer()]) as run:
    drive_traffic()

for report in run.reports:
    print(report.text)
```

`Window` is the scheduled form. It takes the same contents, the
triggers described below, and up to three report destinations, and
`start()` arms it:

```python
window = wrapture.Window(
    name="stats",
    after="5m", duration="30s", every="1h", align=True,
    collect=[wrapture.Aggregate()],
    report="reports/{window}-{first}/run-{run:02}.txt",
    on_report=publish,
)
window.start()
...
window.stop()
```

`open()` and `close()` drive runs by hand between `start()` and
`stop()`; `window.run` is the run in progress, `window.reports` the
most recent reports (the last `retain`, default ten). A window's
runs never overlap: `open()` while a run is in progress is refused
and counted on `refused`.

## Triggers: when a window opens

All optional, combinable within the rules given, durations in the
forms `rotate=` accepts (`"30s"`, `"15m"`, `"1h"`, `"1h30m"`, or a
number of seconds); the config key for `duration` is `for`.

| key | meaning |
|---|---|
| `after = "5m"` | open the first run this long after `start()`; `after = "0s"` is at start |
| `for = "30s"` (`duration=`) | how long a run stays open |
| `every = "1h"` | repeat |
| `times = 24` | cap the repeats (needs `every`) |
| `align = true` | put the repeats on the local wall-clock boundary of `every`: hourly on the hour, daily at midnight; with `after`, the first boundary after the delay (needs `every`) |
| `at = "22:00"` | open the first run at the next local occurrence of a time of day; `every`/`times` continue from there; cannot combine with `after` or `align` |
| `jitter = "10s"` | add a random delay of up to this to each opening, so a fleet of workers does not fire together |
| `on_signal = "SIGUSR2"` | open a run when the process receives the signal (in code a `signal.Signals` member, its number, or its name with or without `SIG`) |
| `on_file = "run/profile-now"` | open a run when the file appears; it is removed as it is consumed, so touch once means run once |

`on_signal` and `on_file` are the two ways an operator opens a run
from outside, without a restart: `kill -USR2 <pid>`, or `touch` a
file, from a shell or a deploy script. A window with either and no
timed trigger opens only when kicked; with `every` as well the kicks
add runs to the schedule. A kick during a timed run (one with `for`)
is refused and counted on `refused`, since runs never overlap; a
kick with no `for` closes the open run and starts the next, so a
signal toggles: kick to start, kick again to report and start over.
The signal handler is installed at `start()` (for a config window,
at apply), which must happen on the main thread, and any handler
already there is called after wrapture's and restored when the last
window listening for that signal stops. `SIGUSR1`/`SIGUSR2` do not
exist on Windows; `on_file` is the cross-platform equivalent, and
the one that works under servers where signal handling is awkward.

Two shortcuts make the commonest shapes the shortest spellings. A
window with no trigger and no `for` is **one run for the whole
process**: it opens at `start()` (for a config window, at apply) and
closes at interpreter exit, one report. `every` without `for` is
**back to back**: each run lasts the whole period, closed as the
next opens, a report at every boundary. `for` shorter than `every`
is the sampled shape; `for` equal to or longer than `every` is
refused, since runs never overlap.

## Reports: what a run produces and where it goes

At each run close every collector produces a `Report`. All reports
share a header, `kind` (the collector type), `name`, `window`, `run`
(numbered from 1 within the window's schedule), `started` and
`ended` (aware local datetimes, offset included), `duration`
(seconds), `cut_short` (the run was closed by `stop()` or
interpreter exit before its scheduled end), and `text`, the
human-readable rendering; then a per-kind `data` payload documented
by each collector. Callbacks and tests reach for `data`; humans read
`text`.

There are three destinations, and a window can use any of them
together:

- **Retention**, always: `window.reports` holds the last `retain`,
  and the run yielded by `window()` holds its own as `run.reports`.
- **`on_report=fn`**, code only: a plain callable given each report,
  on the scheduler thread at run close. An exception from it is
  handled as a sink's would be, suppressed, counted on
  `window.errors` and warned once, so a dashboard hiccup can never
  take down the window or the application.
- **`report="reports/{window}-{datetime}-{pid}.txt"`**, the file
  form and the only one config can spell: an output path template
  (see [Output paths and rotation](ad-hoc-tracing.md#output-paths-and-rotation))
  written once per run per collector, `report.text` in full, temp
  file then rename so a half-written report is never observed. With
  several collectors in one window each file gets the collector's
  name before the extension.

Inside a window three more path variables have values, in report
templates and in the paths of the sinks the window holds:
`{window}` is the window's name, `{first}` is when the schedule's
first run opened (the same for every run of one schedule, so it
groups a batch), and `{run}` is the run number, `{run:02}` for
zero padding. So `reports/{window}-{first}/run-{run:02}.txt` gives
one directory per schedule and one numbered file per run, and a
per-run stream is `peek-{first}/run-{run:02}.log`. A path inside a
repeating window that names the same file for every run (no run or
time variable) rewrites it each time, and is warned about where the
window is built.

## Built-in collectors: Counter and Aggregate

`Counter` and `Aggregate` are introduced on the
[ad-hoc tracing page](ad-hoc-tracing.md#counting-without-retaining);
here are their report shapes. Both take a `name=` (config key
`name`), defaulting to their kind, which is what `{name}` expands to
and what the report header shows. Gating keys apply to them as to
any entry: `filter = { kind = "request" }` on an aggregate reports
requests alone.

**Counter** (`kind = "counter"`): `text` is the header line and the
count; `data` is `{"count": n}`.

```text
counter "queries" run 4, 2026-08-18 14:00:00 to 15:00:00 +10:00 (1h 0m 0s), pid 4142
21,884 operations
```

**Aggregate** (`kind = "aggregate"`): `text` is the header, a totals
line, and a table sorted by self time with one row per path, columns
`calls`, `total`, `self`, `per-call`, `min`, `max`, `path`, plus an
`errors` column when any operation raised; a path begun but never
completed shows its count with the timing cells blank. `data` is
`{"paths": {path: {"count", "completed", "errors", "total", "self",
"min", "max"}}, "begun": n, "completed": n, "raised": n}`, the paths
in table order.

```text
aggregate "stats" run 3, 2026-08-18 14:00:00 to 15:00:00 +10:00 (1h 0m 0s), pid 4142
7 paths, 21,884 operations begun, 21,879 completed, 12 raised

calls    total     self  per-call    min      max  errors  path
9,412  84.211s  31.870s     8.9ms  1.2ms  412.7ms          myapp.wsgi:application
9,412  52.341s  40.106s     5.6ms  0.8ms  388.2ms          myapp.views:OrderView.get
9,401  12.235s  12.235s     1.3ms  0.4ms   96.1ms      12  myapp.services:Pricing.quote
```

The two shapes people usually want are both windows: one report for
the whole process is a window with no trigger and no `for`; a report
every hour with totals reset each hour is `every = "1h"` with
`align = true`, one file per run:

```toml
[[window]]
name = "stats"
report = "reports/stats-{datetime}.txt"

[[window.collect]]
type = "aggregate"

[[window]]
name = "hourly"
every = "1h"
align = true
report = "reports/{window}-{first}/run-{run:02}.txt"

[[window.collect]]
type = "aggregate"
```

Naming a collector in `[[sink]]` is a load-time error pointing here:
a collector needs a run to close before it has anything to report.

## In config: `[[window]]`

Windows are top-level `[[window]]` tables beside `[[sink]]`: the sink
list is what listens all the time, the window list what listens or
collects briefly. Contents go under `[[window.collect]]` in exactly
the sink grammar, `type` plus keys, gating keys included.

```toml
# Hourly two-minute readable trace, one file per run, grouped by
# the schedule's start.
[[window]]
name = "peek"
every = "1h"
align = true
for = "2m"

[[window.collect]]
type = "printer"
path = "peek-{first}/run-{run:02}.log"
timestamps = true

# Always-on JSONLines to disk, plus a live printer only for two
# minutes after a five-minute warm-up.
[[sink]]
type = "jsonlines"
path = "traces/trace-{date}.jsonl"

[[window]]
name = "warmup"
after = "5m"
for = "2m"

[[window.collect]]
type = "printer"
depth = 2

# Operator-triggered: count for a minute when kicked.
[[window]]
name = "kick"
on_signal = "SIGUSR2"
for = "60s"
report = "reports/{window}-{datetime}.txt"

[[window.collect]]
type = "counter"

# Overnight batch: twelve runs, one an hour from 22:00.
[[window]]
name = "overnight"
at = "22:00"
every = "1h"
times = 12
for = "20s"
report = "reports/{window}-{first}/run-{run:02}.txt"

[[window.collect]]
type = "aggregate"
```

Relative paths, `report`, `on_file` and the `path` of a builtin sink
alike, anchor to the config file's directory. `config.report()` lists each
window with its schedule in words. A window without a `name` is
named by position (`window1`); the window variables in a top-level
`[[sink]]` path are a load-time error, since there they have nothing
to name.

## Clocks, time zones and restarts

Everything is local time: `at`, `align`, and the `{date}`/`{time}`
variables, because "22:00" in a config is what an operator means by
22:00 on that machine, and file names, report headers and the
machine's own logs then agree. There is no time zone setting; the
`{utc:...}` path variable is the one UTC affordance, and each report
states its start time with its offset so it is unambiguous alone.

Wall-clock triggers (`at`, aligned `every`) are computed by finding
the next occurrence in local time afresh after each run, never by
adding seconds to the previous one. On the night the clocks go
forward a time that does not exist is skipped to the next day; on
the night they go back the repeated hour fires once. Relative
triggers (`after`, unaligned `every`) and `for` are monotonic
durations, unaffected by clock steps.

Schedules live in the process and start afresh at apply. Nothing is
persisted or resumed: `after` and unaligned `every` are measured
from apply, so a restart shifts that cadence; `at` waits for its
next occurrence and `times` counts runs of this process, so a
restart part-way through an `at` plus `times` batch does not make
the remaining runs. A schedule with no batch to lose is
restart-proof by construction: `every = "1h"` with `align = true`
simply waits for the next hour boundary and carries on, each run
named by its own timestamp. Reach for `at` plus `times` when the
batch itself is the point, knowing a restart ends it early. A clean
shutdown during an open run (interpreter exit, or `wrapture.shutdown()`
called from a host's own shutdown notification) closes it, marks its
report `cut_short` and delivers it; a hard kill loses it.

Pre-fork servers give each worker its own windows, so "one report an
hour" is one per worker per hour; put `{pid}` in the report path and
use `jitter` to spread aligned openings.

## Writing your own collector

A collector satisfies the `Collector` protocol: `arm()` and
`disarm()` are called at run open and close, `report(run)` renders
what was collected as a `Report`, and `reset()` clears it for the
next run. A collector that accumulates from events is a `Sink` as
well; the window registers it as a process sink while it is armed,
so it hears every recorded event from every thread.

```python
class Counting(wrapture.Sink):
    capture_args = "none"
    capture_result = "none"

    def __init__(self):
        self.count = 0

    def arm(self): ...
    def disarm(self): ...
    def reset(self):
        self.count = 0

    def on_enter(self, event):
        self.count += 1

    def report(self, run):
        return wrapture.Report(
            kind="count", name="count", window=run.window, run=run.number,
            started=run.started, ended=run.ended, duration=run.duration,
            text=f"{self.count} operations", data={"count": self.count},
            cut_short=run.cut_short,
        )
```

Errors raised by a collector's methods are handled as sink errors:
suppressed, counted, warned once, and the run carries on.
