"""Exception types raised by wrapture."""


class AlreadyAppliedError(RuntimeError):
    """apply() was called on a binding whose wrapper is already applied."""


class WrongModeError(AttributeError):
    """A behaviour namespace was accessed that does not apply to this
    binding's mode.

    Derives from AttributeError so that hasattr() can be used to probe
    which namespaces a binding supports.
    """


class ExpectationNotMetError(AssertionError):
    """A declared expect_* expectation was not met when the timeline
    verified it at exit.

    Derives from AssertionError so test frameworks report it as a test
    failure rather than an error.
    """


class RecordingGapWarning(RuntimeWarning):
    """An observed operation ran on a thread with no recording context
    while a timeline was active elsewhere, so it is missing from that
    timeline's tape. Behaviour still applied; only recording was lost.
    """


class SinkErrorWarning(RuntimeWarning):
    """A sink raised from one of its notification methods. The error
    was suppressed so the observed application is unaffected; it is
    counted on the sink's `errors` attribute, and this warning is
    emitted for the sink's first failure only."""


class NeverAppliedError(RuntimeError):
    """events was read on a binding that was never applied, so nothing
    could possibly have been recorded for it."""


class NotImplementedYetError(NotImplementedError):
    """A feature whose API shape is settled is not implemented yet."""


class DeferredTargetError(ValueError):
    """A string target used wrapt's trailing `?` deferred-patching syntax,
    which is not supported. Import the module first and bind against it.
    """


class ConfigError(Exception):
    """A tracing configuration could not be loaded or applied: a config
    file said something the schema does not allow, a reference in it did
    not resolve, or applying it to the running process failed."""


class ConfigWarning(UserWarning):
    """A tracing configuration is suspicious but not fatally wrong,
    such as a match pattern that selected no members at all."""
