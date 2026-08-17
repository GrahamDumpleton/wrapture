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
from .exceptions import ConfigWarning


def bootstrap(module: Any = None) -> AppliedConfig | None:
    """Resolve the config precedence chain and apply what it finds.

    Fired as a wrapt post-import hook at interpreter startup when the
    environment opts in; `module` is the trigger module the hook
    convention passes (os, per the entry point) and carries no
    information. Finding no config warns rather than raises: the
    environment variable propagates to every Python process launched
    under it, and taking down an interpreter that merely lacks a
    config file in its working directory would make the opt-in far
    too dangerous. A config that exists but fails to load or apply
    propagates ConfigError loudly, the development posture; the
    production posture is the hardening step's concern.

    Returns the AppliedConfig record, or None when no config was
    found, so what the bootstrap installed is inspectable.
    """

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

    return load_config(source).apply()
