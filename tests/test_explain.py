"""Tests for explain(), the `configured` marker, and the reprs of the
behaviour namespaces."""

import functools
import os
import types
from typing import Any

import wrapture
from wrapture import binding, bindings, iterator
from wrapture.explain import describe_callable, describe_value


class Gateway:
    # Returns Any so tests can compare stubbed values of other types.

    def charge(self, amount: int) -> Any:
        return {"id": f"ch_{amount}", "amount": amount}


class Model:
    status = "draft"


def add_auth_header(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    return args, kwargs


def check_amount(*args: Any, **kwargs: Any) -> None:
    pass


def note_progress(result: Any) -> None:
    pass


def check_transition(value: Any) -> None:
    pass


def acme_only(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    return True


def application(environ: Any, start_response: Any) -> list[bytes]:
    start_response("200 OK", [])
    return [b"ok"]


def force_beta(carrier: Any) -> Any:
    return carrier


def stamp(status: Any, headers: Any) -> Any:
    return status, headers


def app_module() -> types.ModuleType:
    module = types.ModuleType("explain_app")
    module.application = application  # type: ignore[attr-defined]
    return module


# ---------------------------------------------------------------------------
# reprs
# ---------------------------------------------------------------------------


def test_phase_repr_shows_the_binding_state() -> None:
    charge = binding(Gateway, "charge")
    recovered = charge.on_call.then(after=2)

    assert repr(recovered) == (
        "<CallPhase 1 of <Binding '__main__:Gateway.charge' callable unapplied>>"
    ).replace("__main__", __name__)


def test_request_namespace_repr_names_its_kind_and_binding() -> None:
    module = app_module()

    for mode in ("wsgi", "asgi"):
        app = binding(module, "application", mode=mode)
        assert repr(app.on_request) == (
            f"<RequestBehaviour of <Binding 'explain_app:application' {mode}"
            f" unapplied>>"
        )


# ---------------------------------------------------------------------------
# configured
# ---------------------------------------------------------------------------


def test_configured_marks_a_callable_binding_with_behaviour() -> None:
    charge = binding(Gateway, "charge")
    assert not charge.configured
    assert repr(charge).endswith("callable unapplied>")

    charge.on_call.returns({"id": "stub"})
    assert charge.configured
    assert repr(charge).endswith("callable unapplied configured>")

    charge.on_call.reset()
    assert not charge.configured


def test_configured_sees_behaviour_on_a_later_phase_only() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.then(after=1)
    assert not charge.configured

    charge.on_call.then().raises(TimeoutError("down"))
    assert charge.configured


def test_configured_follows_the_state_across_apply_and_suspend() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.raises(TimeoutError("down"))

    with charge:
        assert repr(charge).endswith("callable active configured>")
        charge.suspend()
        assert repr(charge).endswith("callable active suspended configured>")


def test_configured_on_attribute_value_mapping_and_request_bindings() -> None:
    status = binding(Model, "status")
    assert not status.configured
    status.on_delete.rejects()
    assert status.configured

    key = binding(os.environ, item="EXPLAIN_KEY")
    assert not key.configured
    key.overrides("sk_test")
    assert key.configured
    key.hides()
    assert key.configured
    key.passes_through()
    assert not key.configured

    settings = {"currency": "USD"}
    mapping = binding(settings, mode="mapping")
    assert not mapping.configured
    mapping.updates({"tax_rate": 0.0})
    assert mapping.configured

    app = binding(app_module(), "application", mode="wsgi")
    assert not app.configured
    app.on_request.transforms_environ(force_beta)
    assert app.configured
    app.on_request.passes_through()
    assert not app.configured


# ---------------------------------------------------------------------------
# explain: callable bindings
# ---------------------------------------------------------------------------


def test_explain_bare_binding_passes_through() -> None:
    charge = binding(Gateway, "charge")

    assert charge.explain() == f"{charge!r}\npasses through"
    assert charge.on_call.explain() == "passes through"


def test_explain_single_phase_channel_sits_beside_its_name() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.transforms_args(add_auth_header)
    charge.on_call.validates_args(check_amount)
    charge.on_call.raises(TimeoutError("busy"))

    assert charge.on_call.explain() == (
        "transforms args: add_auth_header\n"
        "validates args: check_amount\n"
        "raises: TimeoutError('busy')"
    )
    assert charge.explain() == (
        f"{charge!r}\n"
        "on_call  transforms args: add_auth_header\n"
        "         validates args: check_amount\n"
        "         raises: TimeoutError('busy')"
    )


def test_explain_phased_chain_labels_phases_and_their_exits() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.raises(TimeoutError("busy"))

    recovered = charge.on_call.then(after=2)
    recovered.returns_from(["queued", "running"])
    recovered.validates_result(note_progress)

    settled = recovered.then()
    settled.returns(
        {
            "url": "/orders",
            "status": 200,
            "headers": {"content-type": "application/json", "x-request-id": "8f19b8c6"},
            "body": [{"id": 1}, {"id": 2}, {"id": 3}],
        }
    )

    assert charge.on_call.explain() == (
        "phase 0  raises: TimeoutError('busy')\n"
        "         ends after 2 calls\n"
        "phase 1  validates result: note_progress\n"
        "         returns from: ['queued', 'running']\n"
        "         ends when the sequence is exhausted\n"
        "phase 2  returns: {'url': '/orders', 'status': 200,"
        " 'headers': <dict 2 keys>, 'body': <list 3 items>}\n"
        "now in phase 0"
    )

    # A phase namespace explains its own phase; the whole binding puts
    # the channel name on its own line with the phases beneath it.

    assert recovered.explain() == (
        "validates result: note_progress\n"
        "returns from: ['queued', 'running']\n"
        "ends when the sequence is exhausted"
    )
    assert charge.explain().splitlines()[:3] == [
        repr(charge),
        "on_call",
        "  phase 0  raises: TimeoutError('busy')",
    ]

    # The chain moves on as calls are handled, and the description
    # follows it.

    gateway = Gateway()
    with charge:
        for _ in range(2):
            try:
                gateway.charge(1)
            except TimeoutError:
                pass

        assert gateway.charge(1) == "queued"
        assert charge.on_call.explain().endswith("now in phase 1")


def test_explain_exit_conditions() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.decorates(functools.partial(add_auth_header))
    charge.on_call.then(until=note_progress).raises(ValueError)
    charge.on_call.then().then()

    assert charge.on_call.explain() == (
        "phase 0  decorates: partial(add_auth_header)\n"
        "         ends when note_progress(event) is true\n"
        "phase 1  raises: ValueError\n"
        "         ends on advance()\n"
        "phase 2  passes through\n"
        "now in phase 0"
    )

    # A sequence on the last phase says what exhaustion does.

    charge = binding(Gateway, "charge")
    charge.on_call.returns_from(iter([1, 2]))
    lines = charge.on_call.explain().splitlines()
    assert lines[0].startswith("returns from: <list_iterator")
    assert lines[1] == (
        "ends when the sequence is exhausted, raising SequenceExhaustedError"
    )


def test_explain_lists_the_recording_options() -> None:
    place = binding(
        Gateway,
        "charge",
        when=acme_only,
        tree=True,
        capture=wrapture.redact("card"),
    )
    assert place.explain() == (
        f"{place!r}\n"
        "records when: acme_only, declining the whole tree; capture: redact card\n"
        "passes through"
    )

    detailed = binding(
        Gateway,
        "charge",
        capture_args="summary",
        capture_result="none",
        stack=5,
        leaf=True,
        category="database",
    )
    assert detailed.explain().splitlines()[1] == (
        "capture args: summary; capture result: none; stack: 5; leaf;"
        " category: database"
    )

    silent = binding(Gateway, "charge", when=False)
    assert silent.explain().splitlines()[1] == "records nothing"


# ---------------------------------------------------------------------------
# explain: other modes
# ---------------------------------------------------------------------------


def test_explain_attribute_binding_channels() -> None:
    status = binding(Model, "status")
    status.on_get.returns_from(["draft", "draft"])
    status.on_get.then().passes_through()
    status.on_set.validates(check_transition)
    status.on_delete.rejects()

    assert status.explain() == (
        f"{status!r}\n"
        "on_get\n"
        "  phase 0  returns from: ['draft', 'draft']\n"
        "           ends when the sequence is exhausted\n"
        "  phase 1  passes through\n"
        "  now in phase 0\n"
        "on_set     validates: check_transition\n"
        "on_delete  rejects"
    )


def test_explain_value_binding_says_what_it_holds() -> None:
    key = binding(os.environ, item="EXPLAIN_KEY")
    assert key.explain() == f"{key!r}\npasses through"

    key.overrides("sk_test")
    assert key.explain() == f"{key!r}\nholds: 'sk_test'"

    with key:
        assert key.explain() == f"{key!r}\nholds: 'sk_test'\nputs back: absent"

    key.hides()
    assert key.explain() == f"{key!r}\nholds: absent"


def test_explain_mapping_binding_says_how_it_changes_the_content() -> None:
    settings = {"currency": "USD"}

    merged = binding(settings, mode="mapping").updates({"tax_rate": 0.0})
    assert merged.explain() == f"{merged!r}\nupdates in place: {{'tax_rate': 0.0}}"

    with merged:
        assert merged.explain() == (
            f"{merged!r}\n"
            "updates in place: {'tax_rate': 0.0}\n"
            "puts back: {'currency': 'USD'}"
        )

    replaced = binding(settings, mode="mapping").overrides({"currency": "EUR"})
    assert replaced.explain().splitlines()[1] == "replaces content: {'currency': 'EUR'}"


def test_explain_request_bindings() -> None:
    module = app_module()

    app = binding(module, "application", mode="wsgi")
    app.on_request.transforms_environ(force_beta)
    app.on_request.transforms_response(stamp)
    app.on_request.returns(
        "503 Service Unavailable", [("Retry-After", "30")], [b"down"]
    )

    assert app.explain() == (
        f"{app!r}\n"
        "on_request\n"
        "  transforms environ: force_beta\n"
        "  transforms response: stamp\n"
        "  returns: 503 Service Unavailable, 1 header, body [b'down']"
    )
    assert app.on_request.explain() == (
        "transforms environ: force_beta\n"
        "transforms response: stamp\n"
        "returns: 503 Service Unavailable, 1 header, body [b'down']"
    )

    asgi = binding(
        module,
        "application",
        mode="asgi",
        when=wrapture.filter_requests(ignore={"path": ["/health"]}),
    )
    asgi.on_request.transforms_scope(force_beta)
    asgi.on_request.raises(RuntimeError("down"))

    assert asgi.explain() == (
        f"{asgi!r}\n"
        "records: filter_requests(ignore={'path': ('/health',)})\n"
        "on_request\n"
        "  transforms scope: force_beta\n"
        "  raises: RuntimeError('down')"
    )


def test_explain_iterator_proxy_and_its_channels() -> None:
    watch = iterator()
    assert watch.explain() == f"{watch!r}\npasses through"
    assert watch.on_item.explain() == "passes through"

    watch.on_item.validates_item(check_amount)
    watch.on_item.transforms_item(note_progress)
    watch.on_finish.validates(note_progress)
    watch.on_abandon.notifies(check_amount)

    assert watch.explain() == (
        "<IteratorProxy 4 behaviour(s)>\n"
        "on_item     validates item: check_amount\n"
        "            transforms item: note_progress\n"
        "on_finish   validates: note_progress\n"
        "on_abandon  notifies: check_amount"
    )
    assert watch.on_item.explain() == (
        "validates item: check_amount\ntransforms item: note_progress"
    )

    # A proxy handed to a stage is described beneath that stage, and a
    # namespace of the proxy stands in for it.

    pages = binding(Gateway, "charge")
    pages.on_call.transforms_result(watch.on_item)
    assert pages.on_call.explain() == (
        "transforms result: <IteratorProxy 4 behaviour(s)>\n"
        "  on_item     validates item: check_amount\n"
        "              transforms item: note_progress\n"
        "  on_finish   validates: note_progress\n"
        "  on_abandon  notifies: check_amount"
    )


def test_explain_group_lists_each_member_under_its_name() -> None:
    charge = binding(Gateway, "charge")
    charge.on_call.raises(TimeoutError("down"))

    pinned = bindings(
        api_key=binding(os.environ, item="EXPLAIN_KEY").overrides("sk_test"),
        charge=charge,
    )

    assert pinned.explain() == (
        "<BindingGroup ['api_key', 'charge']>\n"
        f"api_key  {pinned.api_key!r}\n"
        "         holds: 'sk_test'\n"
        f"charge   {charge!r}\n"
        "         on_call  raises: TimeoutError('down')"
    )


# ---------------------------------------------------------------------------
# describing values and callables
# ---------------------------------------------------------------------------


def test_describe_value_spells_out_the_first_level_only() -> None:
    assert describe_value(None) == "None"
    assert describe_value(True) == "True"
    assert describe_value("x" * 5) == "'xxxxx'"
    assert describe_value((1,)) == "(1,)"
    assert describe_value({"a": [1, 2], "b": {"c": 3}}) == (
        "{'a': [1, 2], 'b': {'c': 3}}"
    )

    nested = {"headers": {"content-type": "application/json", "x-id": "1" * 30}}
    assert describe_value(nested) == "{'headers': <dict 2 keys>}"
    assert describe_value([list(range(40))]) == "[<list 40 items>]"

    long_text = "y" * 150
    assert describe_value(long_text) == "'" + "y" * 99 + "...+52"


def test_describe_value_never_raises() -> None:
    class Awkward:
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    assert describe_value(Awkward()) == "<unreprable Awkward: RuntimeError>"

    class Loud(Exception):
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    assert describe_value(Loud()) == "Loud(...)"


def test_describe_callable_names_what_the_caller_wrote() -> None:
    assert describe_callable(add_auth_header) == "add_auth_header"
    assert describe_callable(Gateway.charge) == "Gateway.charge"
    assert describe_callable(Gateway().charge) == "Gateway.charge"
    assert describe_callable(TimeoutError) == "TimeoutError"
    assert describe_callable(functools.partial(check_amount, 1)) == (
        "partial(check_amount)"
    )

    def local(value: Any) -> Any:
        return value

    assert describe_callable(local) == "local"
    assert describe_callable(lambda value: value) == "<lambda>"
