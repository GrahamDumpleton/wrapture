# Testing async code

A notifier fans messages out through a push gateway. The client is
`async def` all the way down: one coroutine method per send, an async
generator streaming delivery receipts back. The notifier's logic is
what the tests are for: does it send to every user, does it survive a
gateway timeout, does it actually await what it calls. That last one is
the bug class unique to async code, a coroutine created and dropped
runs precisely nothing, and the code reads correctly right up until
production.

`unittest.mock` grew a separate class for all this (`AsyncMock`, with
`assert_awaited` alongside `assert_called`). wrapture needs no separate
anything: a binding on an `async def` target delivers stubbed outcomes
on await, exactly as the real method would, and every event already
records the call and its completion as two moments, so "called but
never awaited" is an ordinary filter.

This example uses: [call behaviour](monkey-patching.md#call-behaviour-changing-what-a-call-does),
[recording on a timeline](unit-testing.md#recording-calls-on-a-timeline)
and [filtering and asserting on events](unit-testing.md#filtering-and-asserting-on-events).

## The application code

The client refuses to run in tests, as ever. The notifier sends
sequentially and treats a timeout as a partial result rather than a
failure:

```python
>>> import asyncio
>>> import wrapture

>>> class PushClient:
...     async def send(self, user: str, message: str) -> str:
...         raise RuntimeError("no gateway in tests")
...
...     async def receipts(self, batch: str):
...         raise RuntimeError("no gateway in tests")
...         yield

>>> class Notifier:
...     def __init__(self, client: PushClient) -> None:
...         self.client = client
...
...     async def broadcast(self, users: list[str], message: str) -> int:
...         delivered = 0
...
...         for user in users:
...             try:
...                 await self.client.send(user, message)
...             except TimeoutError:
...                 break
...             delivered += 1
...
...         return delivered

>>> notifier = Notifier(PushClient())

```

## Stubbing an async method: outcomes arrive on await

`returns()` on a binding whose target is `async def` does not return
the value from the call, because the real method would not either: the
call still produces an awaitable, and the value arrives when it is
awaited. The notifier's `await` works unchanged against the stub:

```python
>>> send = wrapture.binding(PushClient, "send")
>>> _ = send.on_call.returns("queued")

>>> with send, wrapture.timeline():
...     asyncio.run(notifier.broadcast(["ana", "ben"], "hello"))
...     send.events.with_args(user="ana").assert_once()
...     send.events.finished().assert_times(2)
2
<EventLog PushClient.send[user='ana']: 1 event(s)>
    PushClient.send(user='ana', message='hello')
<EventLog PushClient.send[finished]: 2 event(s)>
    PushClient.send(user='ana', message='hello')
    PushClient.send(user='ben', message='hello')

```

`raises()` is the same shape: the exception arrives on await, so the
notifier's `except TimeoutError` around the `await` is what handles it,
and the partial-result behaviour is tested with the real control flow:

```python
>>> _ = send.on_call.returns("queued")
>>> _ = send.on_call.then(after=1).raises(TimeoutError("gateway busy"))

>>> with send, wrapture.timeline():
...     asyncio.run(notifier.broadcast(["ana", "ben", "cal"], "hello"))
...     send.events.raising(TimeoutError).assert_once()
1
<EventLog PushClient.send[raising=TimeoutError]: 1 event(s)>
    PushClient.send(user='ben', message='hello')

>>> _ = send.on_call.reset()

```

One user was delivered to, the second send timed out, and the third was
never attempted, all through the notifier's real loop.

## Catching the call that was never awaited

Here is the async bug worth a section of its own. A "fire and forget"
notify that forgets the await compiles, runs, and does nothing:

```python
>>> class FireAndForget(Notifier):
...     async def nudge(self, user: str) -> None:
...         self.client.send(user, "nudge")      # bug: missing await

```

An event has two moments, the call and the completion, and
`Event.finished` says whether the second ever happened. For a coroutine
that means "was it awaited", so the assertion that catches this bug is
one line, `pending()`:

```python
>>> import warnings

>>> _ = send.on_call.returns("queued")

>>> with send, wrapture.timeline() as tape:
...     with warnings.catch_warnings():
...         warnings.simplefilter("ignore", RuntimeWarning)
...         asyncio.run(FireAndForget(PushClient()).nudge("ana"))
...     send.events.assert_once()
...     send.events.pending().assert_once()
...     send.events.finished().assert_never()
<EventLog PushClient.send: 1 event(s)>
    PushClient.send(user='ana', message='nudge')
<EventLog PushClient.send[pending]: 1 event(s)>
    PushClient.send(user='ana', message='nudge')
<EventLog PushClient.send[finished]: 0 event(s)>
    (no events)

```

The send was *called*, which is why the `assert_called()` habit from
`unittest.mock` misses this bug, but it never finished: nothing was
awaited. The tape
counts its open events too, so the smell shows up even without a
targeted assertion:

```python
>>> tape.pending
1

```

Python's own safety net for this is the "coroutine was never awaited"
`RuntimeWarning` when the dropped coroutine is collected, silenced
above to keep the example's output stable. wrapture names that warning
usefully as well: the coroutine a stubbed `async def` hands back
carries the target's name, so the warning says `PushClient.send`, not
the name of some library helper.

## Concurrent sends on one tape

Real notifiers fan out concurrently. Each task's events land on the
shared tape, correctly attributed, so the test asserts on the whole fan
without caring how the scheduler interleaved it:

```python
>>> class ConcurrentNotifier(Notifier):
...     async def broadcast(self, users: list[str], message: str) -> int:
...         results = await asyncio.gather(
...             *(self.client.send(user, message) for user in users),
...         )
...         return len(results)

>>> with send, wrapture.timeline():
...     asyncio.run(ConcurrentNotifier(PushClient()).broadcast(["ana", "ben", "cal"], "hi"))
...     send.events.finished().assert_times(3)
...     sorted(event.arguments["user"] for event in send.events)
3
<EventLog PushClient.send[finished]: 3 event(s)>
    PushClient.send(user='ana', message='hi')
    PushClient.send(user='ben', message='hi')
    PushClient.send(user='cal', message='hi')
['ana', 'ben', 'cal']

```

## An async generator: stubbed items arrive on iteration

The receipts stream is an async generator, and its stub follows the
same rule: the outcome arrives the way the real protocol delivers it.
`returns()` on an async-generator target yields the given items under
`async for`:

```python
>>> receipts = wrapture.binding(PushClient, "receipts")
>>> _ = receipts.on_call.returns(["r-1", "r-2"])

>>> async def collect(client: PushClient) -> list[str]:
...     return [receipt async for receipt in client.receipts("batch-9")]

>>> with receipts, wrapture.timeline():
...     asyncio.run(collect(PushClient()))
...     receipts.events.finished().assert_once()
['r-1', 'r-2']
<EventLog PushClient.receipts[finished]: 1 event(s)>
    PushClient.receipts(batch='batch-9')

```

`raises()` on the same target fails the iteration rather than the
call, which is where an `async for` consumer's error handling actually
lives. A stream consumed halfway stays `pending()`, the same
two-moments rule as everywhere else; the
[streaming data example](example-streaming-data.md) explores that
interplay for generators at length.

## As a pytest suite

`pytest-asyncio` (or anyio) drives the coroutines; the bindings and
assertions are unchanged. The plugin's `tape` fixture spans each test:

```python
import pytest
import wrapture

pytest_plugins = ["wrapture.pytest_plugin"]


@pytest.fixture
def send():
    binding = wrapture.binding(PushClient, "send")
    binding.on_call.returns("queued")

    with binding:
        yield binding


@pytest.mark.asyncio
async def test_every_user_is_sent_to_and_awaited(send):
    delivered = await Notifier(PushClient()).broadcast(["ana", "ben"], "hello")

    assert delivered == 2
    send.events.finished().assert_times(2)
    send.events.pending().assert_never()


@pytest.mark.asyncio
async def test_a_timeout_stops_the_broadcast_early(send):
    send.on_call.then(after=1).raises(TimeoutError("gateway busy"))

    delivered = await Notifier(PushClient()).broadcast(["ana", "ben", "cal"], "hello")

    assert delivered == 1
    send.events.raising(TimeoutError).assert_once()
```

The `pending().assert_never()` line is worth adopting as a habit in
async suites: it costs nothing when everything is awaited, and it is
the line that fails when someone deletes an `await` two years from now.

## Where next

[Call behaviour](monkey-patching.md#call-behaviour-changing-what-a-call-does)
covers async targets in full, including how phases and sequences
interact with awaiting.
[Recording calls on a timeline](unit-testing.md#recording-calls-on-a-timeline)
explains the two moments an event carries and how concurrent tasks nest
on the shared tape, and
[filtering and asserting on events](unit-testing.md#filtering-and-asserting-on-events)
lists `finished()`, `pending()` and the rest of the vocabulary. For
supplying an async stand-in rather than wrapping one, `stub()` takes
`kind="coroutine"` and `mock()` reads each method's kind from its spec;
see [supplying hooks and collaborators](example-supplied-stand-ins.md).
