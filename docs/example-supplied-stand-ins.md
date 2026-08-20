# Supplying hooks and collaborators

A job pipeline publishes work to a message broker. The pipeline class
takes the broker transport through its constructor, and accepts an
`on_complete` hook that it promises to call after each job. The
pipeline logic is yours and is what the tests are for: does it open a
channel, publish the right message, close the channel even on failure,
and honour the hook contract. The transport is what the tests cannot
have, and this time there is nothing to wrap: the seam is the
constructor, and the test itself must supply both the collaborator and
the hook.

This is the territory `unittest.mock` covers with `Mock()`, and the
convenience comes at a price: a fabricated object answers every method,
invented on first touch, so the misspelled method and the drifted call
pass silently. wrapture supplies the same stand-ins as a deliberate,
scoped opt-in that stays strict: [`stub()`](unit-testing.md#supplying-a-stand-in-with-stub)
builds one callable, [`mock()`](unit-testing.md#a-collaborator-double-with-mock)
builds one collaborator from a named class, and both record on the same
timeline as everything else in the test.

This example uses: [stub()](unit-testing.md#supplying-a-stand-in-with-stub),
[mock()](unit-testing.md#a-collaborator-double-with-mock),
[recording on a timeline](unit-testing.md#recording-calls-on-a-timeline),
[filtering and asserting on events](unit-testing.md#filtering-and-asserting-on-events)
and [the call tree and ordering](unit-testing.md#the-call-tree-and-ordering).

## The application code

The transport is the collaborator the pipeline receives. Its methods
are the seam: every one of them does network I/O in production, so here
they refuse to run at all, the same trick the
[external services example](example-external-services.md) plays:

```python
>>> import wrapture

>>> class Channel:
...     def publish(self, body: str, routing_key: str = "jobs") -> None:
...         raise RuntimeError("no broker in tests")
...
...     def close(self) -> None:
...         raise RuntimeError("no broker in tests")

>>> class Transport:
...     def open_channel(self) -> Channel:
...         raise RuntimeError("no broker in tests")
...
...     def close(self) -> None:
...         raise RuntimeError("no broker in tests")

```

The pipeline is the code under test. It opens a channel per batch,
publishes each job, closes the channel whatever happens, and calls the
hook once per job with the job's id and outcome:

```python
>>> class Pipeline:
...     def __init__(self, transport, on_complete=None):
...         self.transport = transport
...         self.on_complete = on_complete
...
...     def run(self, jobs: list[str]) -> int:
...         channel = self.transport.open_channel()
...         sent = 0
...
...         try:
...             for job in jobs:
...                 channel.publish(job)
...                 sent += 1
...
...                 if self.on_complete is not None:
...                     self.on_complete(job, "sent")
...         finally:
...             channel.close()
...
...         return sent

```

## The naive approach: hand-written stand-ins

Without library support the test writes a recorder class for the
transport, another for the channel, and a list-appending function for
the hook, then asserts against the lists. It works, and for a
collaborator with real behaviour it stays the right move. The cost is
everything around it: each stand-in is hand-kept in step with the real
class, records only what its author thought to record, and none of it
shows up on the timeline next to the rest of the test's events. The
temptation is a fabricated object that answers everything, and that is
the trade wrapture declines: the stand-ins below are exactly as wide as
what they stand in for.

## A stub as the hook

The hook is one callable that the pipeline promises to call. A bare
`stub()` accepts any arguments, returns None, and records; the label
names it in events and failure output:

```python
>>> hook = wrapture.stub("on_complete")

```

The transport is not the subject of this first test, so its methods are
stubbed too, one collaborator built from the named class. Every method
of `Transport` becomes a recording stub that returns None until
configured; `open_channel` is configured to return a `Channel` double
so the pipeline has something to publish on:

```python
>>> channel = wrapture.mock(Channel)
>>> transport = wrapture.mock(Transport)
>>> transport.open_channel.returns(channel)
<StubCallable 'Transport.open_channel'>

>>> pipeline = Pipeline(transport, on_complete=hook)

>>> with wrapture.timeline():
...     pipeline.run(["job-1", "job-2"])
...     hook.events.assert_times(2)
...     hook.events.first.args
2
<EventLog on_complete: 2 event(s)>
    on_complete(args=('job-1', 'sent'), kwargs={})
    on_complete(args=('job-2', 'sent'), kwargs={})
('job-1', 'sent')

```

The hook fired once per job, with the arguments the pipeline actually
sent riding on the event (a bare stub accepts anything, so they record
under its `*args`/`**kwargs` rather than by name). Not caring what
arrives is the point of reaching for a bare stub, and it is the
explicit opposite of the package's default strictness.

## Opting the hook back into strictness

The hook contract is worth checking too: the pipeline must call it as
`on_complete(job, outcome)`. `mimics=` borrows a real callable's
signature, so the stub checks each call and records arguments by
parameter name:

```python
>>> def on_complete(job: str, outcome: str) -> None: ...

>>> hook = wrapture.stub(mimics=on_complete)
>>> pipeline = Pipeline(transport, on_complete=hook)

>>> with wrapture.timeline():
...     pipeline.run(["job-1"])
...     hook.events.with_args(job="job-1", outcome="sent").assert_once()
1
<EventLog __main__.on_complete[job='job-1', outcome='sent']: 1 event(s)>
    __main__.on_complete(job='job-1', outcome='sent')

```

If the pipeline drifted, calling the hook with an extra keyword, or
forgetting an argument, the call would raise `TypeError` exactly as the
real hook would, before anything was recorded. Integration drift
between the pipeline and its hooks cannot hide behind the stand-in.

## The collaborator, strictly

The transport double deserves a closer look, because it is where the
difference from a fabricated object shows. A mock requires a spec and
fabricates nothing beyond it: a misspelled method is an
`AttributeError` wherever it happens, in the test or inside the
pipeline:

```python
>>> transport.open_chanel
Traceback (most recent call last):
    ...
AttributeError: Transport has no attribute 'open_chanel'; the mock fabricates nothing beyond its spec

```

Calls are checked against the real method's signature:

```python
>>> channel.publish("job-1", "jobs", "extra")
Traceback (most recent call last):
    ...
TypeError: Channel.publish (stubbed): too many positional arguments

```

And there are no fabricated chains. Had `open_channel` not been
configured, it would have returned None and the pipeline's
`channel.publish(...)` would have failed loudly on the next line,
instead of an invented channel absorbing the call. The object graph a
test depends on is declared, one double per node, which is why the
`Channel` double was built and wired first.

The declared graph is also reachable back from the configuration, so a
fixture need not thread every double through to the test:

```python
>>> transport.open_channel.returns_value is channel
True

```

## Asserting across the whole batch

Each method records events by parameter name, so the assertions read
the same as they would against a real binding, and the tape sees stubs,
mocks and real bindings together. Here the order of one batch is
checked end to end: the channel is opened, both jobs are published, and
the channel is closed after the last publish:

```python
>>> with wrapture.timeline() as tape:
...     pipeline.run(["job-1", "job-2"])
...     channel.publish.events.with_args(body="job-1").assert_once()
...     channel.publish.events.with_args(routing_key="jobs").assert_times(2)
...     tape.assert_order(transport.open_channel, channel.publish,
...                       channel.publish, channel.close)
2
<EventLog Channel.publish[body='job-1']: 1 event(s)>
    Channel.publish(body='job-1', routing_key='jobs')
<EventLog Channel.publish[routing_key='jobs']: 2 event(s)>
    Channel.publish(body='job-1', routing_key='jobs')
    Channel.publish(body='job-2', routing_key='jobs')
<Tape: 6 events>

```

`with_args(routing_key="jobs")` matched both publishes even though the
pipeline never spelled the default out: matching is against the
signature-normalized call. The tape's six events are the four transport
calls plus the hook still wired from the previous section, everything
the test's stand-ins saw, in one recording. `assert_order` steps accept stub and mock
methods directly, alongside bindings and filtered logs; without flags
it is a subsequence check, and `consecutive=True` or `exact=True`
tighten it when the test means "nothing between" or "nothing else at
all".

## Failure paths, reconfigured in place

Every mock method carries the same `returns()` and `raises()` verbs, so
a failure is injected where the test needs it and the pipeline's
clean-up promise is checked: the channel must be closed even when a
publish blows up.

```python
>>> channel.publish.raises(ConnectionError("broker gone"))
<StubCallable 'Channel.publish'>

>>> with wrapture.timeline():
...     try:
...         pipeline.run(["job-1"])
...     except ConnectionError:
...         pass
...     channel.close.events.assert_once()
<EventLog Channel.close: 1 event(s)>
    Channel.close()

>>> channel.publish.returns(None)
<StubCallable 'Channel.publish'>

```

Two doubles of the same class stay apart, each recording its own
events, so a pipeline that opens one channel per priority can be tested
without the calls merging:

```python
>>> fast, slow = wrapture.mock(Channel), wrapture.mock(Channel)

>>> with wrapture.timeline():
...     fast.publish("job-1", routing_key="fast")
...     slow.publish("job-2")
...     fast.publish.events.assert_once()
...     slow.publish.events.with_args(routing_key="jobs").assert_once()
<EventLog Channel.publish: 1 event(s)>
    Channel.publish(body='job-1', routing_key='fast')
<EventLog Channel.publish[routing_key='jobs']: 1 event(s)>
    Channel.publish(body='job-2', routing_key='jobs')

```

## As a pytest suite

The same pieces arranged as fixtures. The doubles are values, so a
fixture builds and wires them; nothing needs removing afterwards,
because nothing was installed anywhere:

```python
import pytest
import wrapture

pytest_plugins = ["wrapture.pytest_plugin"]


@pytest.fixture
def channel():
    return wrapture.mock(Channel)


@pytest.fixture
def transport(channel):
    transport = wrapture.mock(Transport)
    transport.open_channel.returns(channel)
    return transport


def test_each_job_is_published_and_reported(transport, channel, tape):
    hook = wrapture.stub("on_complete")
    pipeline = Pipeline(transport, on_complete=hook)

    assert pipeline.run(["job-1", "job-2"]) == 2

    channel.publish.events.assert_times(2)
    hook.events.assert_times(2)
    tape.assert_order(transport.open_channel, channel.publish,
                      channel.publish, channel.close)


def test_the_channel_is_closed_when_a_publish_fails(transport, channel):
    channel.publish.raises(ConnectionError("broker gone"))
    pipeline = Pipeline(transport, on_complete=wrapture.stub())

    with pytest.raises(ConnectionError):
        pipeline.run(["job-1"])

    channel.close.events.assert_once()
```

The plugin's `tape` fixture spans the test, so the doubles record with
no explicit `timeline()` block, and its leak sweep still guards any
real bindings the suite mixes in alongside them.

## Where next

[Supplying a stand-in with stub()](unit-testing.md#supplying-a-stand-in-with-stub)
covers the outcome verbs, `kind=` for coroutine and generator
stand-ins, and what `mimics=` borrows.
[A collaborator double with mock()](unit-testing.md#a-collaborator-double-with-mock)
states the spec rule and shows substituting a whole class at a location
with a factory in a value binding. The
[comparison page](coming-from-mock.md) maps `unittest.mock`'s `Mock()`,
`create_autospec` and `AsyncMock` onto these pieces, and ends with the
one kind of double wrapture deliberately does not provide. When the collaborator's calls
must be told apart by which object they landed on, see
[filtering and asserting on events](unit-testing.md#filtering-and-asserting-on-events)
for `with_instance()`.
