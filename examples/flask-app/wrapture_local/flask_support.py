"""Instrument Flask, entirely under the covers.

A stand-in for what a wrapture-instrumentation-flask package would
ship: one Instrumentation subclass, triggered by the import of "flask"
itself, that instruments every Flask application the process ever
creates. Nothing here knows the application's module, its attribute
names, or whether it uses the application-factory pattern; packaged
with an entry point, the config would say `[[instrument]]` with
`name = "flask"` and nothing else. Here the config names the class by
reference, `wrapture_local.flask_support:FlaskInstrumentation`, and
everything else is the same.

Three patches, all at Flask's own choke points:

- Flask.__init__ installs the recording WSGI middleware on each new
  instance's wsgi_app attribute, the documented place Flask
  middleware goes, so every request records as one tree however the
  instance was made.
- Flask.add_url_rule substitutes wrapture.observed(view_func) as
  routes register, so every view handler records beneath its
  request, wherever the view came from: module functions, closures,
  blueprints from other modules. observed() is idempotent, so a view
  registered twice is not wrapped twice.
- Flask.handle_exception is the only place a view's exception can be
  seen after Flask catches it: wsgi_app catches around the dispatch
  and hands the exception here, which returns the 500 response, so
  the request itself completes normally. The binding notes the
  exception against the enclosing request event with
  note_exception(), so the request shows the failure beside its
  status: as a `!!` marker on the printed line, and as an exception
  event with error status on the exported span.

All three bindings are created with when=False, making them
behaviour-only: the plumbing is not the trace, so they act without
ever recording their own calls. They are applied as one group whose
remove() is registered with on_remove(), which is what lets
AppliedConfig.revert() and the instrumentation() context manager
take the whole thing down again.
"""

from __future__ import annotations

from typing import Any

import wrapture


class FlaskInstrumentation(wrapture.Instrumentation):
    """Request and view tracing for Flask applications."""

    target = "flask"
    modules = ("flask",)
    removable = True

    def apply(self, name: str, module: Any) -> None:
        def wrap_app(
            wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> Any:
            outcome = wrapped(*args, **kwargs)

            # Label with the app's own import name, so requests read as
            # "myapp.wsgi_app" and two apps in one process stay distinct.

            instance.wsgi_app = wrapture.WSGIMiddleware(
                instance.wsgi_app, label=f"{instance.name}.wsgi_app"
            )
            return outcome

        constructor = wrapture.binding(module.Flask, "__init__", when=False)
        constructor.on_call.decorates(wrap_app)

        def wrap_view(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> tuple[tuple[Any, ...], dict[str, Any]]:
            if kwargs.get("view_func") is not None:
                kwargs = dict(kwargs, view_func=wrapture.observed(kwargs["view_func"]))
            elif len(args) >= 3 and args[2] is not None:
                args = (*args[:2], wrapture.observed(args[2]), *args[3:])

            return args, kwargs

        registrar = wrapture.binding(module.Flask, "add_url_rule", when=False)
        registrar.on_call.transforms_args(wrap_view)

        def note_failure(
            wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> Any:
            # The exception is the handler's one positional argument. Aim
            # the note at the request the middleware recorded, not at this
            # call, which is not recorded anyway; outside a request (a
            # handler invoked with nothing recording) this is a no-op.

            exception = args[0] if args else kwargs.get("e")
            if exception is not None:
                wrapture.note_exception(
                    exception, event=wrapture.current_event(kind="request")
                )

            return wrapped(*args, **kwargs)

        handler = wrapture.binding(module.Flask, "handle_exception", when=False)
        handler.on_call.decorates(note_failure)

        # One group, applied together and registered for removal as a
        # unit: the bindings recipe every instrumentation built on
        # bindings follows.

        group = wrapture.bindings(
            constructor=constructor, registrar=registrar, handler=handler
        )
        group.apply()

        self.on_remove(group.remove)
