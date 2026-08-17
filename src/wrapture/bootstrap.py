"""The autowrapt bootstrap entry point: zero-code injection.

Two opt-ins gate this path, and both live outside wrapture. The
autowrapt package must be installed, whose .pth hook is what makes
interpreter startup do anything at all, and
AUTOWRAPT_BOOTSTRAP=wrapture must be set in the environment. The
variable's value names an entry point group that autowrapt hands to
wrapt.discover_post_import_hooks() once site initialisation
completes: each entry in the group maps a trigger module to a
callback fired when that module is imported. wrapture's entry hooks
the os module, which is always already imported by then, so the
callback fires immediately, at startup, before the application's own
code. Absent either opt-in, the entry point in wrapture's metadata
is inert, and wrapture itself never imports autowrapt.

When it fires, bootstrap() resolves the same config precedence chain
the runner uses and applies what it finds, so patches and sinks are
in place before the application imports anything. The runner and
this trigger are two doorways into the same machinery; everything
they do is the config layer's.
"""

from __future__ import annotations

import warnings
from typing import Any

from .config import AppliedConfig, find_config, load_config
from .exceptions import ConfigError, ConfigWarning

# The record of what the last bootstrap installed. In an injected
# process nothing else holds it, so this is the handle for operator
# code reaching in (a console, a debugger, a signal handler):
# report(), suspend(), resume() and revert() live on it. None when
# the bootstrap has not run or applied nothing.

applied: AppliedConfig | None = None


def bootstrap(module: Any = None) -> AppliedConfig | None:
    """Resolve the config precedence chain and apply what it finds.

    Fired as a wrapt post-import hook at interpreter startup when the
    environment opts in; `module` is the trigger module the hook
    convention passes (os, per the entry point) and carries no
    information. Injection never takes the process down: finding no
    config warns, and a config that exists but cannot be loaded or
    applied warns too, with the process starting untraced either way.
    The environment variable propagates to every Python process
    launched under it, and an error raised here is fatal to the
    interpreter before it has even started, so the loud failures the
    runner and programmatic paths give belong there, not here.

    Returns the AppliedConfig record, or None when nothing was
    applied; the same record is kept on this module's `applied`
    attribute, since in an injected process no other code holds it.
    """

    global applied

    applied = None
    source = find_config()

    if source is None:
        warnings.warn(
            "AUTOWRAPT_BOOTSTRAP=wrapture is set but no config was"
            " found: set WRAPTURE_CONFIG, or provide wrapture.toml or"
            " a [tool.wrapture] table in pyproject.toml in the current"
            " directory. Nothing was traced.",
            ConfigWarning,
            stacklevel=2,
        )
        return None

    try:
        applied = load_config(source).apply()
    except ConfigError as exc:
        warnings.warn(
            f"config {source} could not be applied: {exc}. The process"
            f" starts untraced.",
            ConfigWarning,
            stacklevel=2,
        )
        return None

    return applied
