# Testing generators and streamed results

Code that streams is lazy by design. A paginated client yields one page
at a time, and the consumer decides how far to read: to the end, only
until it finds what it wants, or until something goes wrong halfway.
The behaviour that matters lives in that interplay: how many pages the
consumer really pulled, whether it stopped when it should, whether it
closed the source or left it hanging, and what it did when a page failed
to arrive.

None of that shows in the return value. A test that hands the consumer
a canned list of pages proves it can add up ids and nothing else: a list
is never lazy, cannot be abandoned, and cannot fail between items.
wrapture leaves the real generator in place and works in two layers. A
binding on the generator method records the whole iteration as one
event, with a live item count and a visible difference between an
iteration that finished and one that was dropped. An iterator proxy from
`iterator()` sits inside that and runs your own behaviour per item, at
exhaustion, at abandonment, and on error, including raising a failure at
exactly the item you choose.

This example uses: [bindings](monkey-patching.md#creating-a-binding),
[call behaviour](monkey-patching.md#call-behaviour-changing-what-a-call-does),
[iterators and generators](monkey-patching.md#iterators-and-generators),
[recording calls on a timeline](unit-testing.md#recording-calls-on-a-timeline),
and [filtering and asserting on events](unit-testing.md#filtering-and-asserting-on-events).

## The application code: a paginated catalogue and its consumers

A stand-in for a paginated client. `pages()` is a generator: nothing is
fetched until the consumer asks, and `fetched` counts the pages the
catalogue actually served.

```python
>>> from collections.abc import Callable, Iterator
>>> from typing import Any

>>> Page = dict[str, Any]

>>> class Catalogue:
...     def __init__(self, records: list[dict[str, Any]], page_size: int = 2) -> None:
...         self.records = records
...         self.page_size = page_size
...         self.fetched = 0
...
...     def pages(self, *, cursor: int = 0) -> Iterator[Page]:
...         while cursor < len(self.records):
...             batch = self.records[cursor : cursor + self.page_size]
...             self.fetched += 1
...
...             yield {"cursor": cursor, "items": batch}
...
...             cursor += self.page_size

```

Three consumers with different reading patterns. `collect_ids` reads to
the end. `first_match` returns as soon as it finds an item, dropping the
generator without closing it. `Exporter.write` swallows an `OSError`
mid-stream and reports how many rows it managed to write.

```python
>>> def collect_ids(pages: Iterator[Page]) -> list[int]:
...     ids: list[int] = []
...
...     for page in pages:
...         ids.extend(item["id"] for item in page["items"])
...
...     return ids

>>> def first_match(pages: Iterator[Page], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
...     for page in pages:
...         for item in page["items"]:
...             if predicate(item):
...                 return item
...
...     return None

>>> class Exporter:
...     def write(self, pages: Iterator[Page], out: list[str]) -> int:
...         written = 0
...
...         try:
...             for page in pages:
...                 for item in page["items"]:
...                     out.append(f"{item['id']},{item['name']}")
...                     written += 1
...         except OSError:
...             pass
...
...         return written

>>> records = [{"id": n, "name": f"item-{n}"} for n in range(1, 6)]
>>> catalogue = Catalogue(records)
>>> collect_ids(catalogue.pages())
[1, 2, 3, 4, 5]
>>> catalogue.fetched
3

```

## The naive approach: a canned list of pages

The obvious test builds the pages by hand and passes them in:

```python
canned = [{"cursor": 0, "items": records[:2]}, {"cursor": 2, "items": records[2:4]}]
assert first_match(iter(canned), lambda item: item["id"] == 3) == records[2]
```

It passes, and it says nothing about laziness. The list is fully built
before the consumer starts, so the test cannot tell whether
`first_match` stopped after the second page or read them all, cannot
tell whether it closed the source, and has no way to make page two fail
to arrive. Those are the properties a streaming consumer is written to
have, and this test cannot fail when they are lost.

## Recording the iteration: one event, live item count

Bind the generator method and record inside a timeline. Calling a
generator function records **one event covering the whole iteration**,
not one per page, and the event's `items` field counts what was pulled
through it. When the consumer reads to the end, `result` holds the
generator's return value, `None` here:

```python
>>> import wrapture

>>> pages = wrapture.binding(Catalogue, "pages")

>>> with wrapture.timeline(pages) as tape:
...     collect_ids(catalogue.pages())
...     print(tape.tree())
...     event = pages.events.first
[1, 2, 3, 4, 5]
__main__:Catalogue.pages(cursor=0)  -> None

>>> event.items, event.result
(3, None)

```

Stopping early looks different. `first_match` finds id 3 on the second
page and returns, so the generator is dropped before it is exhausted.
The event closes with the item count reached and no result at all:
`wrapture.MISSING` rather than `None`, and no `->` in the tree, which
is the honest signal that the iteration never finished:

```python
>>> with wrapture.timeline(pages) as tape:
...     first_match(catalogue.pages(), lambda item: item["id"] == 3)
...     print(tape.tree())
...     event = pages.events.first
{'id': 3, 'name': 'item-3'}
__main__:Catalogue.pages(cursor=0)

>>> event.items, event.result is wrapture.MISSING
(2, True)

```

That already answers "how far did it read" and "did it finish" without
touching the consumer. This treatment keys on the bound call returning
a real generator, which `pages()` does; a method returning any other
kind of iterator records it as an ordinary result with no item count
(see [Iteration recording covers generators only](known-limitations.md#iteration-recording-covers-generators-only)).

## Watching each item with an iterator proxy

Item values are deliberately not captured on the tape, so when a test
wants to know *which* pages went by, or to react to each one, it says
so with an iterator proxy. `iterator()` creates a factory with no
target; behaviour is configured on its namespaces, and calling the
factory with a generator returns a wrapped generator applying that
behaviour. `on_item.validates_item()` runs a check per item and passes
the item through unchanged; `on_finish.validates()` runs at normal
exhaustion with the generator's return value; `on_abandon.notifies()`
runs when a started, unexhausted generator is closed, by an explicit
`close()` or by garbage collection:

```python
>>> cursors: list[int] = []
>>> outcomes: list[tuple[str, Any]] = []

>>> watch = wrapture.iterator()
>>> watch.on_item.validates_item(lambda page: cursors.append(page["cursor"]))
<IteratorItemBehaviour of <IteratorProxy 1 behaviour(s)>>
>>> watch.on_finish.validates(lambda value: outcomes.append(("finished", value)))
<IteratorFinishBehaviour of <IteratorProxy 2 behaviour(s)>>
>>> watch.on_abandon.notifies(lambda: outcomes.append(("abandoned", None)))
<IteratorAbandonBehaviour of <IteratorProxy 3 behaviour(s)>>

```

Since the factory is a callable that takes an iterator and returns one,
it slots straight into the binding's `transforms_result()`: every
generator `pages()` returns is wrapped on the way out. Behaviour applies
whenever the binding is applied, timeline or not, so `with pages:` is
enough here:

```python
>>> pages.on_call.transforms_result(watch)
<CallBehaviour of <Binding '__main__:Catalogue.pages' callable unapplied>>

>>> with pages:
...     collect_ids(catalogue.pages())
[1, 2, 3, 4, 5]
>>> cursors, outcomes
([0, 2, 4], [('finished', None)])

```

Run the early-stopping consumer through the same proxy, inside a
timeline this time. The two layers compose: the consumer drives the
recording relay, which drives the proxy, which drives the real
generator, so the event's `items` and the proxy's `cursors` agree. The
difference from `collect_ids` shows up as data: two pages seen, then an
abandonment, because `first_match` dropped the generator and CPython
closed it on the spot. An item stage can also call
`wrapture.annotate()` to pin what it knows onto the very event being
recorded:

```python
>>> def note_cursor(page: Page) -> None:
...     wrapture.annotate(last_cursor=page["cursor"])

>>> watch.on_item.validates_item(note_cursor)
<IteratorItemBehaviour of <IteratorProxy 4 behaviour(s)>>

>>> cursors.clear(); outcomes.clear()

>>> with wrapture.timeline(pages):
...     first_match(catalogue.pages(), lambda item: item["id"] == 3)
...     event = pages.events.first
{'id': 3, 'name': 'item-3'}
>>> cursors, outcomes
([0, 2], [('abandoned', None)])
>>> event.items, event.data, event.result is wrapture.MISSING
(2, {'last_cursor': 2}, True)

```

Reconfiguring the factory affects only generators wrapped afterwards,
so a proxy can be adjusted between calls without re-binding.

## Injecting a failure at page k

A canned list cannot fail between items; a proxy can. An item stage
that raises fails the iteration at that point: the wrapped generator is
closed, `on_error.notifies()` hooks see the exception, and it then
propagates to the consumer exactly as if `pages()` had raised while
producing that page. A small counter picks the item:

```python
>>> def fail_at(position: int, exc: Exception) -> Callable[[Page], None]:
...     seen = 0
...
...     def check(page: Page) -> None:
...         nonlocal seen
...         seen += 1
...
...         if seen == position:
...             raise exc
...
...     return check

>>> errors: list[BaseException] = []

>>> flaky = wrapture.iterator()
>>> flaky.on_item.validates_item(fail_at(2, OSError("connection reset")))
<IteratorItemBehaviour of <IteratorProxy 1 behaviour(s)>>
>>> flaky.on_error.notifies(errors.append)
<IteratorErrorBehaviour of <IteratorProxy 2 behaviour(s)>>

```

Give the binding this proxy instead (`transforms_result` stages
accumulate, so clear the earlier one first) and put the swallowing
exporter under it. The exporter should report the two rows it wrote
before the failure and no more; the timeline shows the iteration ending
in the injected error rather than exhaustion:

```python
>>> pages.on_call.passes_through().transforms_result(flaky)
<CallBehaviour of <Binding '__main__:Catalogue.pages' callable unapplied>>

>>> with wrapture.timeline(pages) as tape:
...     out: list[str] = []
...     Exporter().write(catalogue.pages(), out)
...     print(tape.tree())
...     event = pages.events.raising(OSError).first
2
__main__:Catalogue.pages(cursor=0)  !! OSError

>>> out
['1,item-1', '2,item-2']
>>> errors
[OSError('connection reset')]
>>> event.items, event.exception
(1, OSError('connection reset'))

```

`items` is 1: page two never reached the consumer, because the check
raised in its place. Either the `raising()` filter on the events or the
proxy's own error list can carry the assertion. A consumer that does not
catch the error, such as `collect_ids`, lets it propagate, and a test
asserts that with `pytest.raises(OSError)` as it would any other
failure. Note that the counter in `fail_at` counts items through the
proxy, not through any one generator, so a proxy built with it is good
for one iteration: make a fresh one per test rather than sharing.

## Transforming items on the way through

`on_item.transforms_item()` rewrites each item and hands the rewritten
one on. The real generator keeps running, so this changes only the one
thing the test cares about: here, shrinking every page to a single item
to check that the exporter counts rows and not pages:

```python
>>> thin = wrapture.iterator()
>>> thin.on_item.transforms_item(lambda page: {**page, "items": page["items"][:1]})
<IteratorItemBehaviour of <IteratorProxy 1 behaviour(s)>>

>>> pages.on_call.passes_through().transforms_result(thin)
<CallBehaviour of <Binding '__main__:Catalogue.pages' callable unapplied>>

>>> with pages:
...     Exporter().write(catalogue.pages(), out := [])
3
>>> out
['1,item-1', '3,item-3', '5,item-5']

```

## Proxying on the consumer side: an argument, not a result

So far the proxy has been attached where the generator is produced. It
can equally be attached where one is consumed: `transforms_args()` on
the consumer's binding rewrites the incoming arguments, and the factory
wraps the generator among them. This is the form for when the producer
is not something you can or want to bind (a generator built inline, or
one arriving from outside the code under test), and it works without
touching `Catalogue` at all:

```python
>>> pages.on_call.passes_through()
<CallBehaviour of <Binding '__main__:Catalogue.pages' callable unapplied>>

>>> cursors.clear(); outcomes.clear()

>>> write = wrapture.binding(Exporter, "write")
>>> write.on_call.transforms_args(
...     lambda args, kwargs: ((watch(args[0]), *args[1:]), kwargs)
... )
<CallBehaviour of <Binding '__main__:Exporter.write' callable unapplied>>

>>> with write:
...     Exporter().write(catalogue.pages(), [])
5
>>> cursors, outcomes
([0, 2, 4], [('finished', None)])

```

Recording is different on this side. The event for `Exporter.write`
records the call and its result of 5 as usual, but gains no item count:
the item lifecycle belongs to the generator's own event, which exists
only when the producer is bound too. Bind both when you want both.

## Putting it together in a pytest test

The same moves as a test file, with `fail_at` from above in a helper
module. The test builds its own proxy, so the failure counter starts
fresh, and the timeline scopes the binding so nothing stays applied
afterwards:

```python
import pytest
import wrapture

from catalogue import Catalogue, Exporter
from helpers import fail_at


@pytest.fixture
def catalogue() -> Catalogue:
    records = [{"id": n, "name": f"item-{n}"} for n in range(1, 6)]
    return Catalogue(records)


def test_exporter_keeps_rows_written_before_a_failure(catalogue: Catalogue) -> None:
    flaky = wrapture.iterator()
    flaky.on_item.validates_item(fail_at(2, OSError("connection reset")))

    pages = wrapture.binding(Catalogue, "pages").on_call.transforms_result(flaky)

    with wrapture.timeline(pages):
        out: list[str] = []
        assert Exporter().write(catalogue.pages(), out) == 2

        pages.events.raising(OSError).assert_once()

    assert out == ["1,item-1", "2,item-2"]
    assert catalogue.fetched == 2
```

The consumer's error handling is asserted against a failure that arrived
in the middle of a real iteration: the rows written before it survive,
`raising()` confirms the recorded event carries the injected `OSError`,
and `fetched` confirms the catalogue was never asked for a third page.

## Where next

- [Iterators and generators](monkey-patching.md#iterators-and-generators)
  is the reference for the proxy: the full protocol guarantees, async
  generators and plain iterators, and `decorates()` for wrapping
  conditionally on both sides of a call.
- [Recording calls on a timeline](unit-testing.md#recording-calls-on-a-timeline)
  covers what a generator event records, including `body_duration`
  versus wall time and how calls made inside the body nest under it.
- Streamed HTTP response bodies get the same one-event treatment at the
  request level; see [WSGI tracing](wsgi-tracing.md) and
  [ASGI tracing](asgi-tracing.md).
