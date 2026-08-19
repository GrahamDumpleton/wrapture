"""Tests for value bindings: a slot named with attr= or item=, held while
applied and restored on remove."""

import os
import sys
import types
from typing import Any

import pytest

from wrapture import (
    WrongModeError,
    binding,
    bindings,
    timeline,
)

# A module of the kind an application keeps its settings in, imported
# by name elsewhere. Registered in sys.modules so string targets work.

config: Any = types.ModuleType("wrapture_test_config")
config.TIMEOUT = 30
config.SETTINGS = {"currency": "USD", "retries": 3}
sys.modules["wrapture_test_config"] = config


class Client:
    base_url = "https://api.example"

    def __init__(self) -> None:
        self.timeout = 10


def make_client(prefix: str = "") -> Any:
    return os.environ[f"{prefix}API_KEY"]


# ---------------------------------------------------------------------------
# construction and errors
# ---------------------------------------------------------------------------


def test_a_slot_makes_a_value_binding() -> None:
    timeout = binding("wrapture_test_config", attr="TIMEOUT")
    key = binding(os.environ, item="WRAPTURE_KEY")

    assert timeout.mode == key.mode == "value"
    assert timeout.label == "wrapture_test_config.TIMEOUT"
    assert timeout.path == "wrapture_test_config:TIMEOUT"
    assert key.label == "_Environ['WRAPTURE_KEY']"
    assert binding("os", "environ", item="K").label == "os.environ['K']"
    assert binding("os:environ", item="K").path == "os:environ['K']"
    assert binding(config, "SETTINGS", item="currency").path == (
        "wrapture_test_config:SETTINGS['currency']"
    )
    assert timeout.wrapper is None
    assert repr(timeout) == "<Binding 'wrapture_test_config.TIMEOUT' value unapplied>"


def test_slot_keywords_are_validated() -> None:
    with pytest.raises(TypeError, match="not both"):
        binding(config, attr="TIMEOUT", item="x")

    with pytest.raises(TypeError, match="takes no mode="):
        binding(config, attr="TIMEOUT", mode="callable")

    with pytest.raises(TypeError, match="not a descriptor slot"):
        binding(config, "SETTINGS", item="currency", mode="attribute")

    with pytest.raises(ValueError, match="mode must be"):
        binding(config, "SETTINGS", item="currency", mode="bogus")

    with pytest.raises(ValueError, match="needs a slot"):
        binding(config, "TIMEOUT", mode="value")

    for option in ({"capture": "none"}, {"stack": "caller"}, {"when": False}):
        with pytest.raises(ValueError, match="records nothing"):
            binding(config, attr="TIMEOUT", **option)


def test_a_value_binding_has_no_namespaces_events_or_expectations() -> None:
    timeout = binding(config, attr="TIMEOUT")

    with pytest.raises(WrongModeError, match="overrides\\(\\), hides\\(\\)"):
        _ = timeout.on_call

    with pytest.raises(WrongModeError, match="records nothing"):
        _ = timeout.events

    with pytest.raises(WrongModeError, match="cannot carry an expectation"):
        timeout.expect_once()

    with pytest.raises(WrongModeError, match="only available on a value binding"):
        binding(Client, "base_url").overrides("x")


def test_an_instance_data_attribute_still_needs_a_slot_or_the_class() -> None:
    client = Client()

    with pytest.raises(TypeError, match="use attr='timeout'"):
        binding(client, "timeout").apply()


# ---------------------------------------------------------------------------
# holding values
# ---------------------------------------------------------------------------


def test_environment_variable_set_and_unset() -> None:
    assert "WRAPTURE_KEY" not in os.environ

    with binding(os.environ, item="WRAPTURE_KEY").overrides("sk_test"):
        assert os.environ["WRAPTURE_KEY"] == "sk_test"
        assert os.getenv("WRAPTURE_KEY") == "sk_test"

    assert "WRAPTURE_KEY" not in os.environ

    os.environ["WRAPTURE_KEY"] = "real"
    try:
        with binding(os.environ, item="WRAPTURE_KEY").hides():
            assert "WRAPTURE_KEY" not in os.environ
            assert os.getenv("WRAPTURE_KEY") is None

        assert os.environ["WRAPTURE_KEY"] == "real"
    finally:
        del os.environ["WRAPTURE_KEY"]


def test_a_dict_entry_is_changed_in_place_so_every_holder_sees_it() -> None:
    settings = config.SETTINGS
    same_object_elsewhere = settings

    with binding("wrapture_test_config", "SETTINGS", item="currency").overrides("EUR"):
        assert same_object_elsewhere["currency"] == "EUR"
        assert config.SETTINGS is same_object_elsewhere

    assert same_object_elsewhere["currency"] == "USD"

    with binding(settings, item="retries").hides():
        assert "retries" not in settings

    assert settings["retries"] == 3


def test_a_module_constant() -> None:
    with binding("wrapture_test_config", attr="TIMEOUT").overrides(0.01):
        assert config.TIMEOUT == 0.01

    assert config.TIMEOUT == 30

    with binding(config, attr="TIMEOUT").hides():
        assert not hasattr(config, "TIMEOUT")

    assert config.TIMEOUT == 30


def test_an_instance_attribute_on_one_object_only() -> None:
    client, other = Client(), Client()

    with binding(client, attr="base_url").overrides("http://localhost:9999"):
        assert client.base_url == "http://localhost:9999"
        assert other.base_url == "https://api.example"
        assert Client.base_url == "https://api.example"

    # The value came from the class, so the instance is left without
    # its own copy afterwards rather than a duplicate of the class value.

    assert "base_url" not in vars(client)
    assert client.base_url == "https://api.example"


def test_a_previously_absent_slot_is_deleted_again_on_remove() -> None:
    with binding(config, attr="BRAND_NEW").overrides(1):
        assert config.BRAND_NEW == 1

    assert not hasattr(config, "BRAND_NEW")

    settings = config.SETTINGS
    with binding(settings, item="new_key").overrides("v"):
        assert settings["new_key"] == "v"

    assert "new_key" not in settings


def test_sys_modules_stand_in() -> None:
    fake: Any = types.ModuleType("wrapture_fake_boto3")
    fake.uploaded = []

    with binding(sys.modules, item="wrapture_fake_boto3").overrides(fake):
        import importlib

        assert importlib.import_module("wrapture_fake_boto3") is fake

    assert "wrapture_fake_boto3" not in sys.modules


def test_a_function_replaced_wholesale() -> None:
    def fixed() -> str:
        return "fixed"

    module: Any = types.ModuleType("wrapture_clock")
    module.now = lambda: "real"

    with binding(module, attr="now").overrides(fixed):
        assert module.now() == "fixed"
        assert module.now is fixed

    assert module.now() == "real"


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_apply_without_configuration_then_configure_live() -> None:
    timeout = binding(config, attr="TIMEOUT")

    with timeout:
        assert timeout.applied and timeout.active
        assert config.TIMEOUT == 30

        assert timeout.overrides(5) is timeout
        assert config.TIMEOUT == 5

        assert timeout.hides() is timeout
        assert not hasattr(config, "TIMEOUT")

        assert timeout.passes_through() is timeout
        assert config.TIMEOUT == 30

        timeout.overrides(7)

    assert config.TIMEOUT == 30


def test_suspend_restores_and_resume_writes_again() -> None:
    timeout = binding(config, attr="TIMEOUT").overrides(5)

    with timeout:
        assert config.TIMEOUT == 5

        timeout.suspend()
        assert config.TIMEOUT == 30
        assert timeout.suspended and timeout.active
        assert "suspended" in repr(timeout)

        timeout.overrides(6)  # takes effect on resume, not now
        assert config.TIMEOUT == 30

        timeout.resume()
        assert config.TIMEOUT == 6

    assert config.TIMEOUT == 30


def test_apply_suspended_then_resume() -> None:
    timeout = binding(config, attr="TIMEOUT").overrides(5)
    timeout.apply(suspended=True)
    try:
        assert config.TIMEOUT == 30
        timeout.resume()
        assert config.TIMEOUT == 5
    finally:
        timeout.remove()

    assert config.TIMEOUT == 30


def test_displacement_is_reported_not_repaired() -> None:
    timeout = binding(config, attr="TIMEOUT").overrides(5)

    with timeout:
        config.TIMEOUT = 99
        assert timeout.applied and not timeout.active
        assert "displaced" in repr(timeout)

    # remove() still restores what was there before the binding

    assert config.TIMEOUT == 30

    hidden = binding(config, attr="TIMEOUT").hides()
    with hidden:
        assert hidden.active
        config.TIMEOUT = 1
        assert not hidden.active

    assert config.TIMEOUT == 30


def test_equal_but_not_identical_values_still_count_as_active() -> None:
    # os.environ hands back fresh str objects, so identity cannot be
    # the test.

    key = binding(os.environ, item="WRAPTURE_KEY").overrides("sk_" + "test")

    with key:
        assert key.active


def test_remove_is_idempotent_and_the_binding_reusable() -> None:
    timeout = binding(config, attr="TIMEOUT").overrides(5)

    for _ in range(3):
        timeout.apply()
        assert config.TIMEOUT == 5
        timeout.remove()
        timeout.remove()
        assert config.TIMEOUT == 30


def test_applying_twice_is_refused() -> None:
    from wrapture import AlreadyAppliedError

    timeout = binding(config, attr="TIMEOUT").overrides(5)
    with timeout:
        with pytest.raises(AlreadyAppliedError):
            timeout.apply()


def test_a_group_of_values_applies_and_removes_together() -> None:
    settings = config.SETTINGS

    env = bindings(
        key=binding(os.environ, item="WRAPTURE_KEY").overrides("sk_test"),
        currency=binding(settings, item="currency").overrides("EUR"),
        timeout=binding(config, attr="TIMEOUT").hides(),
    )

    with env:
        assert os.environ["WRAPTURE_KEY"] == "sk_test"
        assert settings["currency"] == "EUR"
        assert not hasattr(config, "TIMEOUT")
        assert env.active

    assert "WRAPTURE_KEY" not in os.environ
    assert settings["currency"] == "USD"
    assert config.TIMEOUT == 30


def test_a_value_binding_records_nothing_inside_a_timeline() -> None:
    timeout = binding(config, attr="TIMEOUT").overrides(5)

    with timeline(timeout) as tape:
        assert config.TIMEOUT == 5
        config.TIMEOUT = 6

    assert tape.all == []
    assert config.TIMEOUT == 30


def test_the_leak_sweep_sees_an_applied_value_binding() -> None:
    from wrapture.bindings import _applied_bindings

    timeout = binding(config, attr="TIMEOUT").overrides(5)
    timeout.apply()
    try:
        assert timeout in _applied_bindings
    finally:
        timeout.remove()

    assert timeout not in _applied_bindings


def test_the_fixture_shape() -> None:
    # One binding applied for the test, a verb per case.

    key = binding(os.environ, item="WRAPTURE_API_KEY")

    with key:
        key.overrides("sk_test")
        assert make_client("WRAPTURE_") == "sk_test"

        key.hides()
        with pytest.raises(KeyError):
            make_client("WRAPTURE_")

    assert "WRAPTURE_API_KEY" not in os.environ


def test_os_environ_rejects_non_strings_as_it_would_by_hand() -> None:
    key = binding(os.environ, item="WRAPTURE_KEY").overrides(5)

    with pytest.raises(TypeError):
        key.apply()

    assert not key.applied


# ---------------------------------------------------------------------------
# callable, wsgi and asgi modes on a mapping entry
# ---------------------------------------------------------------------------


def handle_get(request: dict[str, Any]) -> str:
    return f"got {request['path']}"


HANDLERS: dict[str, Any] = {"GET": handle_get}


def test_a_callable_in_a_mapping_is_wrapped_in_place() -> None:
    from wrapture import Binding

    handler = binding(sys.modules[__name__], "HANDLERS", item="GET", mode="callable")
    assert handler.mode == "callable"
    assert handler.label == f"{__name__}.HANDLERS['GET']"
    assert handler.path == f"{__name__}:HANDLERS['GET']"

    handler.on_call.raises(RuntimeError("unavailable"))
    handler.on_call.then(after=2).passes_through()

    original = HANDLERS["GET"]

    with handler, timeline() as tape:
        assert handler.active
        assert handler.wrapper is HANDLERS["GET"]
        assert isinstance(handler, Binding)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                HANDLERS["GET"]({"path": "/a"})

        assert HANDLERS["GET"]({"path": "/b"}) == "got /b"

    assert HANDLERS["GET"] is original
    assert not handler.applied

    events = tape.for_binding(handler)
    events.in_phase(0).assert_times(2)
    events.in_phase(1).with_args(request={"path": "/b"}).assert_once()


def test_a_wrapped_mapping_entry_is_strict_and_records_arguments() -> None:
    handler = binding(HANDLERS, item="GET", mode="callable")
    handler.on_call.returns("stub")

    with handler:
        assert HANDLERS["GET"]({"path": "/x"}) == "stub"

        with pytest.raises(TypeError, match="stubbed"):
            HANDLERS["GET"]({"path": "/x"}, bogus=1)


def test_wrapping_a_missing_entry_is_an_error_and_displacement_is_reported() -> None:
    absent = binding(HANDLERS, item="PUT", mode="callable")

    with pytest.raises(KeyError, match="no entry 'PUT'"):
        absent.apply()

    handler = binding(HANDLERS, item="GET", mode="callable")
    original = HANDLERS["GET"]

    with handler:
        HANDLERS["GET"] = handle_get  # someone rebinds the entry
        assert handler.applied and not handler.active
        assert "displaced" in repr(handler)

    # remove() left the rebinding alone rather than clobbering it

    assert HANDLERS["GET"] is original


def test_a_wsgi_application_in_a_registry() -> None:
    def site(environ: dict[str, Any], start_response: Any) -> Any:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"hello"]

    registry: dict[str, Any] = {"site": site}
    app = binding(registry, item="site", mode="wsgi")

    def serve(environ: dict[str, Any]) -> bytes:
        status: list[str] = []
        body = registry["site"](environ, lambda s, h, e=None: status.append(s))
        return b"".join(body)

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/hello",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.url_scheme": "http",
    }

    with app, timeline() as tape:
        assert app.active
        assert serve(environ) == b"hello"

    assert registry["site"] is site

    (event,) = tape.all
    assert event.kind == "request"
    assert event.result == "200 OK"


def test_an_asgi_application_in_a_registry() -> None:
    import asyncio

    async def site(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi"})

    registry: dict[str, Any] = {"site": site}
    app = binding(registry, item="site", mode="asgi")

    async def serve() -> list[Any]:
        sent: list[Any] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
        await registry["site"](scope, receive, send)
        return sent

    with app, timeline() as tape:
        sent = asyncio.run(serve())

    assert registry["site"] is site
    assert sent[0]["status"] == 200

    (event,) = tape.all
    assert event.kind == "request"


def test_item_with_a_recording_option_needs_a_recording_mode() -> None:
    with pytest.raises(ValueError, match="records nothing"):
        binding(HANDLERS, item="GET", capture="none")

    handler = binding(HANDLERS, item="GET", mode="callable", capture="none")
    assert handler.mode == "callable"
