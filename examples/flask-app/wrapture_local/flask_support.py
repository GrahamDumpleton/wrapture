"""Point Flask's dispatch at the observed forms of its view functions.

Flask captures a reference to each view function at the moment
@app.route runs, while the application module is still importing. The
config's observe entries wrap the module attributes, but Flask's
view_functions table still holds the originals it captured, so
dispatch would bypass the observation entirely.

This setup hook closes that gap. Post-import hooks fire in the order
they were registered, and a config registers its observe entries
before its setup entries, so by the time this runs the module
attributes are the wrapped forms; pointing view_functions back at
them routes dispatch through the observation. Views defined
elsewhere, such as Flask's own static handler, are left untouched.
"""

import sys
from typing import Any


def instrument(module: Any) -> None:
    """Repoint the module's Flask app at its observed view functions."""

    app = module.app

    for endpoint, view in list(app.view_functions.items()):
        owner = sys.modules.get(getattr(view, "__module__", ""))
        replacement = getattr(owner, view.__name__, None) if owner else None

        if replacement is not None and replacement is not view:
            app.view_functions[endpoint] = replacement
