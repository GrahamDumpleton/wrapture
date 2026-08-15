"""Tests for the opt-in pytest plugin, run through pytester.

Each test writes a small pytest suite to a temporary directory,
activates the plugin the way a user would (pytest_plugins in a
conftest), runs it, and asserts on the outcomes and output.
"""

import pytest

pytestmark = pytest.mark.usefixtures("pytester")


def _activate(pytester: pytest.Pytester) -> None:
    pytester.makeconftest('pytest_plugins = ["wrapture.pytest_plugin"]')


def test_a_leaked_binding_fails_the_test_and_is_removed(
    pytester: pytest.Pytester,
) -> None:
    _activate(pytester)
    pytester.makepyfile(
        """
        import wrapture

        class Gateway:
            def charge(self, amount):
                return amount

        def test_leaks():
            wrapture.binding(Gateway, "charge").apply()

        def test_world_was_repaired():
            import wrapt
            attr = vars(Gateway)["charge"]
            assert not issubclass(type(attr), wrapt.FunctionWrapper)
        """
    )

    result = pytester.runpytest()

    # The sweep raises in teardown, so the leaking test reports as an
    # error after its call phase passed.

    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*bindings left applied after the test*"])
    result.stdout.fnmatch_lines(["*Gateway.charge*"])


def test_wider_scoped_fixtures_are_not_flagged(pytester: pytest.Pytester) -> None:
    _activate(pytester)
    pytester.makepyfile(
        """
        import pytest
        import wrapture

        class Gateway:
            def charge(self, amount):
                return amount

        @pytest.fixture(scope="module")
        def stubbed():
            with wrapture.binding(Gateway, "charge").on_call.returns(0) as stub:
                yield stub

        def test_one(stubbed):
            assert Gateway().charge(5) == 0

        def test_two(stubbed):
            assert Gateway().charge(5) == 0
        """
    )

    result = pytester.runpytest()
    result.assert_outcomes(passed=2)


def test_a_test_scoped_fixture_that_cleans_up_passes(
    pytester: pytest.Pytester,
) -> None:
    _activate(pytester)
    pytester.makepyfile(
        """
        import pytest
        import wrapture

        class Gateway:
            def charge(self, amount):
                return amount

        @pytest.fixture
        def stubbed():
            with wrapture.binding(Gateway, "charge").on_call.returns(0) as stub:
                yield stub

        def test_uses_the_stub(stubbed):
            assert Gateway().charge(5) == 0
        """
    )

    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_the_tape_fixture_records_across_the_test(
    pytester: pytest.Pytester,
) -> None:
    _activate(pytester)
    pytester.makepyfile(
        """
        import wrapture

        class Gateway:
            def charge(self, amount):
                return amount

        def test_records(tape):
            with wrapture.binding(Gateway, "charge") as charge:
                Gateway().charge(5)

                charge.events.with_args(amount=5).assert_once()

            assert len(tape.all) == 1
        """
    )

    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_a_failing_test_gets_the_tape_attached(pytester: pytest.Pytester) -> None:
    _activate(pytester)
    pytester.makepyfile(
        """
        import wrapture

        class Gateway:
            def charge(self, amount):
                return amount

        def test_fails_with_a_tape(tape):
            with wrapture.binding(Gateway, "charge"):
                Gateway().charge(5)
                assert False, "deliberate"
        """
    )

    result = pytester.runpytest()

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*wrapture tape*"])
    result.stdout.fnmatch_lines(["*Gateway.charge(amount=5)*"])


def test_a_passing_test_attaches_nothing(pytester: pytest.Pytester) -> None:
    _activate(pytester)
    pytester.makepyfile(
        """
        import wrapture

        class Gateway:
            def charge(self, amount):
                return amount

        def test_passes(tape):
            with wrapture.binding(Gateway, "charge"):
                Gateway().charge(5)
        """
    )

    result = pytester.runpytest("-v")

    result.assert_outcomes(passed=1)
    assert "wrapture tape" not in result.stdout.str()


def test_eventlog_comparisons_are_explained(pytester: pytest.Pytester) -> None:
    _activate(pytester)
    pytester.makepyfile(
        """
        import wrapture

        class Gateway:
            def charge(self, amount):
                return amount

        def test_compares_a_log(tape):
            with wrapture.binding(Gateway, "charge") as charge:
                Gateway().charge(5)

                assert charge.events.with_args(amount=999) == "something"
        """
    )

    result = pytester.runpytest()

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*filtered from:*"])
    result.stdout.fnmatch_lines(["*Gateway.charge(amount=5)*"])
