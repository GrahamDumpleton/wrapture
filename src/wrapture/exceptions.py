"""Exception types raised by wrapture."""


class AlreadyAppliedError(RuntimeError):
    """apply() was called on a binding whose wrapper is already applied."""


class WrongModeError(AttributeError):
    """A behaviour namespace was accessed that does not apply to this
    binding's mode.

    Derives from AttributeError so that hasattr() can be used to probe
    which namespaces a binding supports.
    """


class NotImplementedYetError(NotImplementedError):
    """A feature whose API shape is settled is not implemented yet."""


class DeferredTargetError(ValueError):
    """A string target used wrapt's trailing `?` deferred-patching syntax,
    which is not supported. Import the module first and bind against it.
    """
