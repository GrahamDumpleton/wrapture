"""Tests for attribute-mode bindings."""

from typing import Any, ClassVar

import pytest

import wrapture
from wrapture import binding


class Model:
    status = "new"  # plain class default

    def __init__(self) -> None:
        self.count = 0


class Priced:
    def __init__(self) -> None:
        self._price = 100

    @property
    def price(self) -> int:
        return self._price

    @price.setter
    def price(self, value: int) -> None:
        if value < 0:
            raise ValueError("negative price")
        self._price = value

    @price.deleter
    def price(self) -> None:
        self._price = 0


# ---------------------------------------------------------------------------
# passthrough semantics: applied with no behaviour, nothing changes
# ---------------------------------------------------------------------------


def test_applied_binding_with_no_behaviour_is_transparent() -> None:
    status = binding(Model, "status")

    with status:
        model = Model()
        assert model.status == "new"  # class default read

        model.status = "sent"
        assert model.status == "sent"  # instance value beats default
        assert vars(model)["status"] == "sent"

        del model.status
        assert model.status == "new"  # default visible again

        with pytest.raises(AttributeError):
            del model.status  # nothing left to delete

    assert vars(Model)["status"] == "new"  # restored exactly


def test_class_level_access_returns_the_descriptor() -> None:
    status = binding(Model, "status").apply()
    try:
        from wrapture.attributes import AttributeDescriptor

        assert type(vars(Model)["status"]) is AttributeDescriptor
        assert Model.status == "new"  # proxy delegates to the prior
    finally:
        status.remove()


def test_property_keeps_working_beneath_the_binding() -> None:
    price = binding(Priced, "price")

    with price:
        priced = Priced()
        assert priced.price == 100  # getter runs
        priced.price = 50  # setter runs
        assert priced.price == 50
        with pytest.raises(ValueError, match="negative"):
            priced.price = -1  # setter validation honoured
        del priced.price  # deleter runs
        assert priced.price == 0

    assert isinstance(vars(Priced)["price"], property)  # restored


# ---------------------------------------------------------------------------
# get behaviour
# ---------------------------------------------------------------------------


def test_get_returns_replaces_the_value() -> None:
    status = binding(Model, "status").on_get.returns("stub")

    with status:
        assert Model().status == "stub"

    assert Model().status == "new"


def test_get_transforms_rewrites_the_value_read() -> None:
    status = binding(Model, "status").on_get.transforms(str.upper)

    with status:
        model = Model()
        assert model.status == "NEW"
        model.status = "sent"
        assert model.status == "SENT"  # instance value transformed too
        assert vars(model)["status"] == "sent"  # stored value untouched


def test_get_validates_observes_the_value() -> None:
    seen: list[str] = []
    status = binding(Model, "status").on_get.validates(seen.append)

    with status:
        assert Model().status == "new"

    assert seen == ["new"]


def test_get_raises_injects_a_failure() -> None:
    status = binding(Model, "status").on_get.raises(RuntimeError("no reads"))

    with status:
        with pytest.raises(RuntimeError, match="no reads"):
            _ = Model().status


def test_get_decorates_controls_the_read() -> None:
    status = binding(Model, "status")

    def around(read: Any, instance: Any) -> str:
        return f"<{read()}:{type(instance).__name__}>"

    status.on_get.decorates(around)

    with status:
        assert Model().status == "<new:Model>"


# ---------------------------------------------------------------------------
# set behaviour
# ---------------------------------------------------------------------------


def test_set_transforms_rewrites_the_value_written() -> None:
    status = binding(Model, "status").on_set.transforms(str.lower)

    with status:
        model = Model()
        model.status = "SENT"
        assert vars(model)["status"] == "sent"


def test_set_validates_can_reject() -> None:
    def known(value: str) -> None:
        assert value in ("new", "sent"), f"unknown status: {value}"

    status = binding(Model, "status").on_set.validates(known)

    with status:
        model = Model()
        model.status = "sent"
        with pytest.raises(AssertionError, match="unknown status"):
            model.status = "bogus"
        assert model.status == "sent"  # rejected write did not land


def test_set_rejects_makes_the_attribute_read_only() -> None:
    status = binding(Model, "status").on_set.rejects()

    with status:
        model = Model()
        assert model.status == "new"  # reads unaffected
        with pytest.raises(AttributeError, match="can't set"):
            model.status = "sent"


def test_set_decorates_controls_the_write() -> None:
    written: list[tuple[str, str]] = []
    status = binding(Model, "status")

    def around(write: Any, instance: Any, value: str) -> None:
        written.append((type(instance).__name__, value))
        write(value)

    status.on_set.decorates(around)

    with status:
        model = Model()
        model.status = "sent"
        assert model.status == "sent"

    assert written == [("Model", "sent")]


# ---------------------------------------------------------------------------
# delete behaviour
# ---------------------------------------------------------------------------


def test_delete_rejects_prevents_deletion() -> None:
    status = binding(Model, "status").on_delete.rejects()

    with status:
        model = Model()
        model.status = "sent"
        with pytest.raises(AttributeError, match="can't delete"):
            del model.status
        assert model.status == "sent"


def test_delete_validates_observes_the_instance() -> None:
    seen: list[Any] = []
    status = binding(Model, "status").on_delete.validates(seen.append)

    with status:
        model = Model()
        model.status = "sent"
        del model.status
        assert model.status == "new"

    assert seen == [model]


def test_delete_decorates_controls_the_delete() -> None:
    status = binding(Model, "status")

    def around(erase: Any, instance: Any) -> None:
        instance.status_archive = instance.status
        erase()

    status.on_delete.decorates(around)

    with status:
        model = Model()
        model.status = "sent"
        del model.status
        assert model.status_archive == "sent"  # type: ignore[attr-defined]
        assert model.status == "new"


# ---------------------------------------------------------------------------
# operations are independent
# ---------------------------------------------------------------------------


def test_operations_configure_independently() -> None:
    status = binding(Model, "status")
    status.on_get.transforms(str.upper)
    status.on_set.rejects()

    with status:
        model = Model()
        assert model.status == "NEW"
        with pytest.raises(AttributeError):
            model.status = "sent"

        status.on_set.passes_through()  # clears set only
        model.status = "sent"
        assert model.status == "SENT"  # get behaviour still applies


# ---------------------------------------------------------------------------
# missing attributes
# ---------------------------------------------------------------------------


def test_missing_ok_binds_an_instance_only_attribute() -> None:
    class Session:
        def __init__(self) -> None:
            self.token = "abc"

    seen: list[str] = []
    token = binding(Session, "token", missing_ok=True)
    token.on_set.validates(seen.append)

    with token:
        session = Session()  # __init__ write goes through the binding
        assert session.token == "abc"
        session.token = "xyz"

    assert seen == ["abc", "xyz"]
    assert "token" not in vars(Session)  # no residue on the class


def test_missing_attribute_reads_raise_until_written() -> None:
    class Bare:
        pass

    value = binding(Bare, "value", missing_ok=True)

    with value:
        bare = Bare()
        with pytest.raises(AttributeError):
            _ = bare.value  # type: ignore[attr-defined]
        bare.value = 1  # type: ignore[attr-defined]
        assert bare.value == 1  # type: ignore[attr-defined]

    assert "value" not in vars(Bare)


def test_explicit_mode_typo_surfaces_at_apply() -> None:
    # An explicit mode= skips creation-time detection, so a misspelled
    # name must fail at apply() instead, for both modes.

    attr = binding(Model, "sttaus", mode="attribute")
    with pytest.raises(AttributeError, match="sttaus"):
        attr.apply()
    assert not attr.applied

    call = binding(Model, "sttaus", mode="callable")
    with pytest.raises(AttributeError):
        call.apply()
    assert not call.applied


# ---------------------------------------------------------------------------
# inherited attributes
# ---------------------------------------------------------------------------


def test_inherited_default_is_found_and_restored() -> None:
    class Base:
        colour = "red"

    class Derived(Base):
        pass

    colour = binding(Derived, "colour").on_get.transforms(str.upper)

    with colour:
        assert Derived().colour == "RED"  # inherited default beneath
        assert "colour" in vars(Derived)  # shadowing descriptor installed
        assert Base().colour == "red"  # base class untouched

    assert "colour" not in vars(Derived)  # shadow removed, not restored
    assert Derived().colour == "red"


# ---------------------------------------------------------------------------
# suspension
# ---------------------------------------------------------------------------


def test_suspended_attribute_binding_is_transparent_and_counts() -> None:
    status = binding(Model, "status").on_get.returns("stub").apply()
    try:
        model = Model()
        assert model.status == "stub"

        status.suspend()
        assert model.status == "new"  # real read
        model.status = "sent"  # real write
        del model.status  # real delete
        assert status.suspended_calls == 3

        status.resume()
        assert model.status == "stub"
    finally:
        status.remove()


# ---------------------------------------------------------------------------
# lifecycle parity with callable mode
# ---------------------------------------------------------------------------


def test_attribute_binding_lifecycle_and_state() -> None:
    status = binding(Model, "status")
    assert not status.applied and not status.active

    status.apply()
    try:
        assert status.applied and status.active
        assert "active" in repr(status)
    finally:
        status.remove()

    assert not status.active
    assert vars(Model)["status"] == "new"

    # reusable across cycles
    with status:
        assert status.active


def test_attribute_binding_reports_displacement() -> None:
    status = binding(Model, "status").apply()
    try:
        original = vars(Model)["status"]
        Model.status = "hijacked"
        assert status.applied
        assert not status.active
        assert "displaced" in repr(status)
    finally:
        Model.status = "new"
        status.remove()
    assert vars(Model)["status"] == "new"
    del original


def test_two_attribute_bindings_compose() -> None:
    outer = binding(Model, "status").on_get.transforms(lambda v: f"outer({v})")
    inner = binding(Model, "status").on_get.transforms(lambda v: f"inner({v})")

    inner.apply()
    outer.apply()
    try:
        assert Model().status == "outer(inner(new))"
    finally:
        # remove in application order: the buried one is spliced out
        inner.remove()
        assert Model().status == "outer(new)"
        outer.remove()

    assert Model().status == "new"
    assert vars(Model)["status"] == "new"


# ---------------------------------------------------------------------------
# combining attribute and callable bindings on one method
# ---------------------------------------------------------------------------


class Service:
    def ping(self) -> str:
        return "pong"


def test_attribute_and_callable_bindings_stack_on_a_method() -> None:
    # An attribute binding on a method observes the access that produces
    # the bound method; a callable binding observes the call. Stacked,
    # both fire. The attribute binding needs an explicit mode=, since
    # detection classifies a method as callable.

    reads: list[Any] = []
    access = binding(Service, "ping", mode="attribute")
    access.on_get.validates(reads.append)

    calls = binding(Service, "ping").on_call.transforms_result(str.upper)

    access.apply()
    calls.apply()
    try:
        service = Service()
        assert service.ping() == "PONG"  # call behaviour ran
        assert len(reads) == 1  # and the access was observed

        _ = service.ping  # bare access, no call
        assert len(reads) == 2
    finally:
        calls.remove()
        access.remove()

    assert Service().ping() == "pong"
    assert vars(Service)["ping"].__name__ == "ping"  # fully restored


def test_stacked_method_bindings_remove_in_either_order() -> None:
    reads: list[Any] = []
    calls = binding(Service, "ping").on_call.transforms_result(str.upper)
    access = binding(Service, "ping", mode="attribute")
    access.on_get.validates(reads.append)

    # Opposite stacking order to the test above, and the buried callable
    # wrapper is removed first, exercising the splice path.

    calls.apply()
    access.apply()
    try:
        assert Service().ping() == "PONG"
        assert len(reads) == 1

        calls.remove()
        assert Service().ping() == "pong"  # call behaviour gone
        assert len(reads) == 2  # access still observed
    finally:
        access.remove()

    assert Service().ping() == "pong"


def test_on_get_can_decorate_the_bound_method_on_the_fly() -> None:
    # The pattern documented in docs/monkey-patching.md: minting a wrapt
    # FunctionWrapper for the bound method at each access, so decoration
    # is decided per access with the instance in hand.

    import wrapt

    class Gateway:
        audited = False

        def charge(self, amount: int) -> dict[str, Any]:
            return {"id": f"ch_{amount}", "amount": amount}

    seen: list[tuple[Any, tuple[Any, ...]]] = []

    def audit(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        seen.append((instance, args))
        return wrapped(*args, **kwargs)

    def selective(read: Any, instance: Any) -> Any:
        bound = read()
        if instance.audited:
            return wrapt.FunctionWrapper(bound, audit)
        return bound

    charge = binding(Gateway, "charge", mode="attribute")
    charge.on_get.decorates(selective)

    with charge:
        flagged, unflagged = Gateway(), Gateway()
        flagged.audited = True

        assert flagged.charge(1) == {"id": "ch_1", "amount": 1}
        assert unflagged.charge(2) == {"id": "ch_2", "amount": 2}

        # wrapt extracts __self__ from the bound method, so the wrapper
        # saw the right instance, and only for the flagged one.
        assert seen == [(flagged, (1,))]

        # a stored bound method stays decorated when called later
        callback = flagged.charge
        callback(3)
        assert seen[-1] == (flagged, (3,))

    assert flagged.charge(4) == {"id": "ch_4", "amount": 4}  # restored
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# unsupported targets
# ---------------------------------------------------------------------------


def test_module_attribute_binding_is_refused() -> None:
    import types

    module = types.ModuleType("wrapture_attr_sample")
    vars(module)["setting"] = True

    setting = binding(module, "setting")
    assert setting.mode == "attribute"

    with pytest.raises(wrapture.NotImplementedYetError, match="module"):
        setting.apply()


def test_slots_member_is_attribute_mode() -> None:
    class Point:
        __slots__ = ("x",)

        x: int

    x = binding(Point, "x")
    assert x.mode == "attribute"

    x.on_set.transforms(lambda v: v * 2)
    with x:
        point = Point()
        point.x = 5
        assert point.x == 10

    point2 = Point()
    point2.x = 5
    assert point2.x == 5  # restored


# ---------------------------------------------------------------------------
# group with mixed modes
# ---------------------------------------------------------------------------


def test_group_mixes_callable_and_attribute_bindings() -> None:
    class Ledger:
        rate: ClassVar[float] = 0.05

        def record(self, entry: str) -> str:
            return f"led-{entry}"

    group = wrapture.bindings(
        rate=binding(Ledger, "rate"), record=binding(Ledger, "record")
    )
    group.rate.on_get.returns(0.10)
    group.record.on_call.returns("led-stub")

    with group:
        ledger = Ledger()
        assert ledger.rate == 0.10
        assert ledger.record("x") == "led-stub"

    assert Ledger().rate == 0.05
    assert Ledger().record("x") == "led-x"
