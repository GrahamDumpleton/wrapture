"""Instrument FastAPI, entirely under the covers.

A stand-in for what a wrapture-fastapi package would ship: one
handler, triggered by the import of "fastapi" itself, that
instruments every FastAPI application the process ever creates.
Nothing here knows the application's module, its attribute names, or
how many applications the process makes; packaged with an entry
point, the config would say `[[setup]]` with `group =
"wrapture_fastapi"` and nothing else.

Two patches, both at FastAPI's own choke points. A FastAPI instance
is itself the ASGI application, so there is no swappable attribute
like Flask's wsgi_app; what there is instead is the middleware
pipeline the instance builds once, lazily, at its first request:

- FastAPI.build_middleware_stack wraps the pipeline it built in the
  recording ASGI middleware, so every request records as one tree,
  however the instance was made and whatever middleware it added.
- APIRouter.add_api_route substitutes wrapture.observed(endpoint) as
  routes register, so every handler records beneath its request,
  wherever it came from: decorators on the app, APIRouter instances,
  include_router from other modules. observed() is idempotent, so a
  handler registered twice is not wrapped twice.

Both bindings are created with when=False, making them behaviour-only:
the plumbing is not the trace, so they transform without ever
recording their own calls.
"""

from __future__ import annotations

from typing import Any

import wrapture


def instrument(module: Any) -> None:
    """Instrument every FastAPI application this process creates."""

    def wrap_stack(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        # Label with the app's own title, so requests read as
        # "myapp.app" and two apps in one process stay distinct.

        stack = wrapped(*args, **kwargs)
        return wrapture.ASGIMiddleware(stack, label=f"{instance.title}.app")

    stack_builder = wrapture.binding(
        module.FastAPI, "build_middleware_stack", when=False
    )
    stack_builder.on_call.decorates(wrap_stack).apply()

    def wrap_endpoint(
        args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if kwargs.get("endpoint") is not None:
            kwargs = dict(kwargs, endpoint=wrapture.observed(kwargs["endpoint"]))
        elif len(args) >= 2 and args[1] is not None:
            args = (args[0], wrapture.observed(args[1]), *args[2:])

        return args, kwargs

    registrar = wrapture.binding(module.APIRouter, "add_api_route", when=False)
    registrar.on_call.transforms_args(wrap_endpoint).apply()
