# Checking that resources are released

Code that acquires a resource, a database connection, a file handle, a
lock, a pooled client, has to give it back on every path out: the
normal return, the early return, and the exception. The acquire is
easy to see, and so is the release that sits at the end of the happy
path. The path that forgets is the one nobody looks at, and it does
not fail. The test passes, the leaked connection is collected
eventually or never, and the pool runs dry a week later in production.

This is hard to test from the outside because the failure is an
absence. Nothing raises, nothing returns the wrong value; one call
that should have happened did not. Asserting on absence needs a record
of what did happen at both ends of the pairing, on the real objects,
including objects a factory minted mid-call that the test never held.

wrapture gives you that record without touching the code: bind the
acquire and the release on their classes, record both onto one
timeline, and pair them up. When something is left open, stack
capture on the acquire names the line that took it. This example
uses: [recording on a timeline](unit-testing.md#recording-calls-on-a-timeline),
[filtering and asserting on events](unit-testing.md#filtering-and-asserting-on-events),
[the call tree](unit-testing.md#the-call-tree-and-ordering),
[attribute bindings](monkey-patching.md#attribute-bindings),
[binding modes](monkey-patching.md#binding-modes-call-versus-attribute),
stack capture, and the [Counter collector](scheduled-tracing.md#built-in-collectors-counter-and-aggregate)
inside a [window](scheduled-tracing.md#in-code-window-and-window).

## The application: a database, connections, and a repository

A stand-in for any pooled resource. `Database.connect()` mints a
`Connection`; a connection answers queries until `close()` sets its
`closed` flag:

```python
>>> import wrapture

>>> class Connection:
...     def __init__(self, number: int) -> None:
...         self.number = number
...         self.closed = False
...
...     def execute(self, sql: str) -> list[tuple[int, str]]:
...         if self.closed:
...             raise RuntimeError("connection is closed")
...
...         return [(1, "widget")] if "id = 1" in sql else []
...
...     def close(self) -> None:
...         self.closed = True
...
...     def __repr__(self) -> str:
...         return f"<Connection {self.number}>"

>>> class Database:
...     def __init__(self) -> None:
...         self.issued = 0
...
...     def connect(self) -> Connection:
...         self.issued += 1
...
...         return Connection(self.issued)

```

The repository is the code under test. `count()` releases in a
`finally`, so it is safe on every path. `find()` releases only on the
path where a row was found; the not-found early return leaks its
connection:

```python
>>> class Repository:
...     def __init__(self, database: Database) -> None:
...         self.database = database
...
...     def count(self, table: str) -> int:
...         connection = self.database.connect()
...
...         try:
...             return len(connection.execute(f"SELECT * FROM {table}"))
...         finally:
...             connection.close()
...
...     def find(self, table: str, key: int) -> tuple[int, str] | None:
...         connection = self.database.connect()
...
...         rows = connection.execute(f"SELECT * FROM {table} WHERE id = {key}")
...         if not rows:
...             return None
...
...         connection.close()
...         return rows[0]

>>> def report(repository: Repository, keys: list[int]) -> tuple[int, list[tuple[int, str]]]:
...     found = [repository.find("products", key) for key in keys]
...
...     return repository.count("products"), [row for row in found if row]

>>> report(Repository(Database()), [1, 2])
(0, [(1, 'widget')])

```

The report is correct. Nothing about that result says a connection was
left open.

## The naive approach: a fake database that keeps a list

The usual move is a hand-written fake `Database` whose `connect()`
appends to a list and whose connections flip a flag, then a test that
walks the list. It works, but it tests a substitute: the real
`Database` and `Connection` never run, the fake has to be kept in step
with them, and every acquiring class in the codebase needs its own
fake. The record you want is of the real calls.

## Recording acquire and release on one timeline

Bind `connect` on `Database` and `close` on `Connection`, and record
both onto one tape. Neither binding changes behaviour; they observe:

```python
>>> connect = wrapture.binding(Database, "connect")
>>> close = wrapture.binding(Connection, "close")

>>> with wrapture.timeline(connect, close) as tape:
...     _ = report(Repository(Database()), [1, 2])
...     print(tape.tree())
Database.connect()  -> <Connection 1>
Connection.close()  -> None
Database.connect()  -> <Connection 2>
Database.connect()  -> <Connection 3>
Connection.close()  -> None

```

Three acquisitions, two releases, and reading down the tape you can
already see which acquisition has no partner. That is the whole
question, answered by counting:

```python
>>> with wrapture.timeline(connect, close):
...     _ = report(Repository(Database()), [1, 2])
...     connect.events.count, close.events.count
(3, 2)

```

Notice that `close` is bound on the `Connection` class, not on any
connection object. The connections do not exist when the test starts;
`connect()` mints them mid-call. A binding on the class wraps the
method for every instance, present and future, which is exactly what
covers objects a factory hands out. The class does have to be imported
already when the binding is created, since a binding holds the wrapper
it installs; only the config file's `[[observe]]` entries, at the end
of this page, bind late when the module arrives. (An attribute binding
on a single instance is refused for a related reason: it installs on
the class, so it would affect every instance; see
[known limitations](known-limitations.md#attribute-bindings-install-on-the-class-never-one-instance).)

## Pairing each acquisition with its release

Counts say something leaked. To say what leaked, pair the events. A
`connect` event's `result` is the connection it minted, and a `close`
event's `instance` is the connection it was called on, so the leaked
connections are the difference between the two sets:

```python
>>> with wrapture.timeline(connect, close):
...     _ = report(Repository(Database()), [1, 2])
...     acquired = {event.result for event in connect.events}
...     released = {event.instance for event in close.events}
...     acquired - released
{<Connection 2>}

```

To say who leaked it, add the repository methods to the timeline. The
tape then nests each acquire and release under the method that made
it, and `tape.children_of()` walks the tree, so a method whose
children include a `connect` but no `close` names itself:

```python
>>> find = wrapture.binding(Repository, "find")
>>> count = wrapture.binding(Repository, "count")

>>> with wrapture.timeline(find, count, connect, close) as tape:
...     _ = report(Repository(Database()), [1, 2])
...     print(tape.tree())
...     for caller in tape.roots():
...         labels = [child.label for child in tape.children_of(caller)]
...         if "Connection.close" not in labels:
...             print("leaked by", caller)
Repository.find(table='products', key=1)  -> (1, 'widget')
  Database.connect()  -> <Connection 1>
  Connection.close()  -> None
Repository.find(table='products', key=2)  -> None
  Database.connect()  -> <Connection 2>
Repository.count(table='products')  -> 0
  Database.connect()  -> <Connection 3>
  Connection.close()  -> None
leaked by Repository.find(table='products', key=2)

```

The tree shows the bug as it happened: `find()` with a key that
matched released its connection, `find()` with a key that did not
match never called `close()`, and `count()` released on the way out
of its `finally`. Nothing here needed the repository to be written
with observation in mind.

## Watching the closed flag itself

Sometimes the release is not a method but a state change: `close()`
sets `closed = True`, and `execute()` reads it. An attribute binding
records reads and writes of the flag as `get` and `set` events on the
same tape. `closed` is assigned in `__init__` rather than defined on
the class, so the binding takes `missing_ok=True`, and the writes made
in `__init__` are recorded too:

```python
>>> closed = wrapture.binding(Connection, "closed", missing_ok=True)

>>> with wrapture.timeline(connect, closed):
...     _ = report(Repository(Database()), [1, 2])
...     connect.events.count, closed.events.of_kind("get").count
...     print(closed.events.of_kind("set").with_value(True))
(3, 3)
<EventLog Connection.closed[set][value=True]: 2 event(s)>
    set Connection.closed = True
    set Connection.closed = True

```

Three connections were acquired, `execute()` read the flag three
times, and two connections were marked closed: the same answer, read
from the state rather than the call. The attribute
binding also carries the `on_set` and `on_get` namespaces, so the same
binding that watches the flag can enforce a rule about it, such as
`on_set.decorates()` with a handler that refuses to write `False` over
a `True`, so a closed connection can never be quietly reopened; the
[attribute bindings](monkey-patching.md#attribute-bindings) reference
has the full set.

## Naming the line that acquired without releasing

The tree names the method; when the method is long, or acquires in
several places, you want the line. Stack capture on the acquire
binding records the calling frame with each `connect` event, and
`stack_frames()` resolves it. Combined with the pairing above, the
report is "this connection was acquired here and never released":

```python
>>> connect = wrapture.binding(Database, "connect", stack="caller")

>>> with wrapture.timeline(connect, close):
...     _ = report(Repository(Database()), [1, 2])
...     released = {event.instance for event in close.events}
...     for event in connect.events:
...         if event.result not in released:
...             frame = wrapture.stack_frames(event.stack)[0]
...             print(f"{event.result} acquired at line {frame.lineno} in {frame.function}, never released")
<Connection 2> acquired at line 14 in Repository.find, never released

```

`stack="caller"` captures one frame, the code that made the call, with
wrapture's own machinery elided, and costs a few hundred nanoseconds
per event. It is priced per binding, so only the acquire pays for it.

## Counting acquisitions against releases without keeping events

For a whole suite, or a long soak, keeping every event is more than
the question needs. Two `Counter` collectors, each behind a `Filter`
that admits one label, count acquisitions and releases as numbers and
retain nothing. Inside a `window()` they report when the run closes:

```python
>>> acquisitions = wrapture.Filter(lambda event: event.label == "Database.connect", wrapture.Counter("acquired"))
>>> releases = wrapture.Filter(lambda event: event.label == "Connection.close", wrapture.Counter("released"))

>>> with connect, close, wrapture.window(collect=[acquisitions, releases]) as run:
...     _ = report(Repository(Database()), [1, 2, 3])
...     _ = report(Repository(Database()), [1])

>>> for entry in run.reports:
...     print(entry.name, entry.data)
acquired {'count': 6}
released {'count': 4}

```

The window applies no bindings of its own, so the bindings are entered
alongside it. Both counters declare that they capture no values, so
while they are the only listeners recording skips value capture
altogether, which is what makes this cheap enough to leave running.

## The same check as a pytest test

In a test the pairing becomes the assertion, and the failure message
carries the leaked connections and where each was acquired. `close` is
given a declared expectation of at least one call, verified when the
timeline exits, so a path that acquires nothing at all cannot pass by
accident:

```python
import wrapture


def test_find_releases_its_connection():
    connect = wrapture.binding(Database, "connect", stack="caller")
    close = wrapture.binding(Connection, "close").expect_at_least(1)

    with wrapture.timeline(connect, close):
        report(Repository(Database()), [1, 2])

        released = {event.instance for event in close.events}
        leaked = [
            (event.result, wrapture.stack_frames(event.stack)[0])
            for event in connect.events
            if event.result not in released
        ]

        assert not leaked, f"connections left open: {leaked}"
```

The test fails today, with output naming `<Connection 2>` and the frame
inside `find()`; fix the early return with a `finally` and it passes.
With the [pytest plugin](unit-testing.md#the-pytest-plugin) enabled,
the tape's tree is attached to the failure report as well.

## The same counters from a config file

The observe-and-count arrangement, minus the pairing, translates
directly to a config file for a running process: two `[[observe]]`
entries and one window holding two filtered counters, reporting when
the process exits or on whatever trigger you give it:

```toml
[[observe]]
target = "myapp.db:Database"
name = "connect"

[[observe]]
target = "myapp.db:Connection"
name = "close"

[[window]]
name = "connections"
report = "reports/connections-{datetime}.txt"

[[window.collect]]
type = "counter"
name = "acquired"
filter = { label = "Database.connect" }

[[window.collect]]
type = "counter"
name = "released"
filter = { label = "Connection.close" }
```

Two numbers that should be equal, one file per run, no code changed.

## Where next

- [Recording calls on a timeline](unit-testing.md#recording-calls-on-a-timeline)
  covers what each event carries, capture policies, and stack capture
  in full.
- [Attribute bindings](monkey-patching.md#attribute-bindings) has the
  complete `on_get`, `on_set` and `on_delete` namespaces.
- [Counting without retaining](ad-hoc-tracing.md#counting-without-retaining)
  shows a `Counter` as a whole-suite query budget, and
  [scheduled tracing](scheduled-tracing.md) covers windows, triggers
  and reports.
- Iterators and generators, another place resources are commonly held
  open, have their own treatment in
  [Iterators and generators](monkey-patching.md#iterators-and-generators).
