"""Tests for observed(): recording proxies for bare callables."""

import functools
import inspect
from collections.abc import Iterator
from typing import Any

import pytest

import wrapture
from wrapture import ObservedCallable, binding, observed, redact, timeline


class Gateway:
    def charge(self, amount: int) -> str:
        return f"ch_{amount}"


gateway = Gateway()


def greet(name: str, punctuation: str = "!") -> str:
    return f"hello {name}{punctuation}"


def relay(name: str) -> str:
    return gateway.charge(7) and greet(name)


def counting(limit: int) -> Iterator[int]:
    yield from range(limit)


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def test_calls_record_with_a_derived_path_and_label() -> None:
    wrapped = observed(greet)

    with timeline() as tape:
        assert wrapped("world") == "hello world!"

    (event,) = tape.all
    assert event.kind == "call"
    assert event.path == f"{__name__}:greet"
    assert event.label == f"{__name__}.greet"
    assert event.arguments == {"name": "world", "punctuation": "!"}
    assert event.result == "hello world!"


def test_label_can_be_overridden() -> None:
    wrapped = observed(greet, label="greeter")

    with timeline() as tape:
        wrapped("world")

    assert tape.all[0].label == "greeter"


def test_the_factory_form_captures_the_function_below() -> None:
    # Called with only options, observed() returns the decorator, so
    # both spellings work: bare @observed on a def, and
    # @observed(label=...) when options are wanted.

    @observed(label="quiet greeter", capture=redact("name"))
    def whisper(name: str) -> str:
        return f"psst {name}"

    assert isinstance(whisper, ObservedCallable)

    with timeline() as tape:
        whisper("secret")

    event = tape.all[0]
    assert event.label == "quiet greeter"
    assert event.arguments is not None
    assert event.arguments["name"] != "secret"


def test_the_factory_form_options_apply() -> None:
    @observed(when=False)
    def silent() -> str:
        return "s"

    @observed
    def bare() -> str:
        return "b"

    with timeline() as tape:
        assert silent() == "s"
        assert bare() == "b"

    (event,) = tape.all
    assert event.label is not None and event.label.endswith(".bare")


def test_the_factory_form_dedupes_by_label() -> None:
    wrapped = observed(greet, label="agent:greet")
    again = observed(label="agent:greet")(wrapped)

    assert again is wrapped


def test_events_nest_around_observed_calls() -> None:
    # An observed callable participates in the tree exactly as a bound
    # one: bindings fired inside it nest under it, and it nests under
    # whatever called it.

    wrapped = observed(relay)
    charge = binding(Gateway, "charge")

    with charge, timeline() as tape:
        wrapped("world")

    outer, inner = tape.all
    assert outer.binding is wrapped
    assert inner.parent_id == outer.seq


def test_exceptions_are_recorded_and_raised() -> None:
    def failing() -> None:
        raise RuntimeError("boom")

    wrapped = observed(failing)

    with timeline() as tape:
        with pytest.raises(RuntimeError, match="boom"):
            wrapped()

    assert isinstance(tape.all[0].exception, RuntimeError)


def test_a_generator_outcome_records_iteration() -> None:
    wrapped = observed(counting)

    with timeline() as tape:
        assert list(wrapped(3)) == [0, 1, 2]

    event = tape.all[0]
    assert event.items == 3
    assert event.result is None  # a plain generator's return value


def test_not_recording_calls_straight_through() -> None:
    wrapped = observed(greet)

    assert wrapped("quiet") == "hello quiet!"


def test_capture_options_apply() -> None:
    wrapped = observed(greet, capture=redact("name"))

    with timeline() as tape:
        wrapped("secret")

    assert tape.all[0].arguments == {"name": "<redacted>", "punctuation": "!"}


def test_when_declines_recording_but_calls_through() -> None:
    wrapped = observed(greet, when=lambda _, args, kwargs: args[0] != "nobody")

    with timeline() as tape:
        assert wrapped("nobody") == "hello nobody!"
        assert wrapped("world") == "hello world!"

    assert len(tape.all) == 1
    assert wrapped.filtered_calls == 1


def test_when_false_never_records_and_counts_nothing() -> None:
    wrapped = observed(greet, when=False)

    with timeline() as tape:
        assert wrapped("quiet") == "hello quiet!"

    assert len(tape.all) == 0
    assert wrapped.filtered_calls == 0


# ---------------------------------------------------------------------------
# lifecycle and assertions
# ---------------------------------------------------------------------------


def test_suspend_and_resume() -> None:
    wrapped = observed(greet)

    with timeline() as tape:
        wrapped.suspend()
        assert wrapped("quiet") == "hello quiet!"
        wrapped.resume()
        wrapped("loud")

    assert len(tape.all) == 1
    assert wrapped.suspended_calls == 1


def test_events_property_filters_to_this_proxy() -> None:
    wrapped = observed(greet)
    other = observed(counting)

    with timeline():
        wrapped("world")
        list(other(2))

        wrapped.events.assert_once()
        other.events.assert_once()


def test_events_outside_a_timeline_raises() -> None:
    wrapped = observed(greet)

    with pytest.raises(RuntimeError, match="inside a timeline"):
        _ = wrapped.events


# ---------------------------------------------------------------------------
# transparency
# ---------------------------------------------------------------------------


def test_the_proxy_is_transparent() -> None:
    wrapped = observed(greet)

    assert wrapped.__name__ == "greet"
    assert wrapped == greet
    assert list(inspect.signature(wrapped).parameters) == ["name", "punctuation"]


def test_a_coroutine_function_is_still_detected_as_one() -> None:
    async def fetch() -> str:
        return "data"

    wrapped = observed(fetch)

    assert inspect.iscoroutinefunction(wrapped)


def test_observed_is_idempotent_per_label() -> None:
    # The label identifies the observation; with none given the
    # derived name serves, so the wrap-in-place idiom re-runs safely.

    wrapped = observed(greet)

    assert observed(wrapped) is wrapped


def test_the_same_label_is_applied_only_once() -> None:
    # Re-applying an existing label returns the callable unchanged;
    # the second call's options do not reconfigure the observation.

    wrapped = observed(greet, label="agent:greet")
    again = observed(wrapped, label="agent:greet", when=False)

    assert again is wrapped

    with timeline() as tape:
        wrapped("world")

    assert len(tape.all) == 1


def test_dedupe_sees_through_foreign_wrappers() -> None:
    # Detection follows the full __wrapped__ chain via wrapt's
    # wrapper_chain(), so an observation stays deduplicated even after
    # a later decorator buries it, and what was given is returned
    # unchanged, the outer wrapper intact.

    inner = observed(greet, label="agent:greet")

    @functools.wraps(inner)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        return inner(*args, **kwargs)

    unchanged: Any = observed(decorated, label="agent:greet")
    assert unchanged is decorated


def test_distinct_labels_stack_and_both_record() -> None:
    # Two observations of one callable are two layers, each recording
    # its own event, nested. An accidental double wrap under different
    # labels is indistinguishable from this, so it is not an error;
    # the double counting is the visible symptom.

    first = observed(greet, label="apm:greet")
    second = observed(first, label="audit:greet")

    assert second is not first

    with timeline() as tape:
        second("world")

    outer, inner = tape.all
    assert outer.label == "audit:greet"
    assert inner.label == "apm:greet"
    assert inner.parent_id == outer.seq


def test_observed_requires_a_callable() -> None:
    with pytest.raises(TypeError, match="wraps a callable"):
        observed(42)  # type: ignore[call-overload]


def test_the_proxy_type_is_exported() -> None:
    assert isinstance(observed(greet), ObservedCallable)
    assert isinstance(observed(greet), wrapture.ObservedCallable)


# ---------------------------------------------------------------------------
# placement on a class: the proxy binds as a method
# ---------------------------------------------------------------------------


def task_hook(self: Any, task_id: str, args: tuple[Any, ...]) -> None:
    pass


def test_placed_on_a_class_the_proxy_binds_and_records_the_instance() -> None:
    hook = observed(task_hook, label="before_start")

    class Task:
        before_start: Any = hook

    task = Task()
    with timeline():
        task.before_start("id-1", (2, 2))

        event = hook.events.assert_once().first
        assert event.instance is task
        assert event.arguments == {"task_id": "id-1", "args": (2, 2)}


def test_bound_access_reaches_the_proxy_state_and_events() -> None:
    hook = observed(task_hook, label="state_hook")

    class Task:
        before_start: Any = hook

    task = Task()
    with timeline():
        task.before_start.suspend()
        task.before_start("id-1", ())
        task.before_start.resume()
        task.before_start("id-2", ())

        assert hook.suspended_calls == 1
        task.before_start.events.assert_once()


def test_bound_signature_drops_self() -> None:
    hook = observed(task_hook, label="signature_hook")

    class Task:
        before_start: Any = hook

    import inspect

    assert list(inspect.signature(Task().before_start).parameters) == [
        "task_id",
        "args",
    ]


def test_when_receives_the_bound_instance() -> None:
    seen: list[Any] = []

    def only_flagged(
        instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> bool:
        seen.append(instance)
        return getattr(instance, "flagged", True)

    hook = observed(task_hook, label="when_hook", when=only_flagged)

    class Task:
        flagged: bool
        before_start: Any = hook

    flagged = Task()
    flagged.flagged = True
    quiet = Task()
    quiet.flagged = False

    with timeline():
        flagged.before_start("id-1", ())
        quiet.before_start("id-2", ())

        assert seen == [flagged, quiet]
        assert hook.events.assert_once().first.instance is flagged
        assert hook.filtered_calls == 1


def test_wrapping_a_bound_method_records_its_instance() -> None:
    class Gateway:
        def charge(self, amount: int) -> int:
            return amount

    gateway = Gateway()
    charge = observed(gateway.charge)

    with timeline():
        charge(500)

        event = charge.events.assert_once().first
        assert event.instance is gateway
        assert event.arguments == {"amount": 500}


def test_the_proxy_stays_weak_referenceable() -> None:
    import weakref

    proxy = observed(task_hook, label="weak_hook")
    assert weakref.ref(proxy)() is proxy
