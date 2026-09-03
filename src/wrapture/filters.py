"""Declarative request filters for the request modes' when= option.

filter_requests() builds a predicate over the fields a request event
records, for the middleware constructors and mode="wsgi"/"asgi"
bindings to accept in place of a hand-written callable, and for a
config file's observe entry to express as a table. The fields are
computed here from the environ or scope exactly as the middlewares
record them, so a filter matches what the event would have said.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from typing import Any

# The fields a filter may name: the request event's plain data fields,
# as the middlewares record them. `interface` is constant for a given
# middleware and `query` passes through redaction, so neither is a
# filter field.

REQUEST_FIELDS: tuple[str, ...] = ("method", "path", "scheme", "protocol", "remote")

FieldTable = Mapping[str, "str | Sequence[str]"]


def _environ_fields(environ: Mapping[str, Any]) -> dict[str, Any]:
    """The filterable fields of a WSGI request, valued as the request
    event records them: the path is SCRIPT_NAME plus PATH_INFO, never
    REQUEST_URI."""

    return {
        "method": environ.get("REQUEST_METHOD", ""),
        "path": environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", ""),
        "scheme": environ.get("wsgi.url_scheme", ""),
        "protocol": environ.get("SERVER_PROTOCOL", ""),
        "remote": environ.get("REMOTE_ADDR", ""),
    }


def _scope_fields(scope: Mapping[str, Any]) -> dict[str, Any]:
    """The filterable fields of an ASGI request, valued as the request
    event records them: scope["path"] is already the full decoded
    path, and the protocol is spelt as WSGI spells it."""

    client = scope.get("client")
    http_version = scope.get("http_version", "")

    return {
        "method": scope.get("method", ""),
        "path": scope.get("path", ""),
        "scheme": scope.get("scheme", ""),
        "protocol": f"HTTP/{http_version}" if http_version else "",
        "remote": client[0] if client else "",
    }


_FIELDS_OF: dict[str, Callable[[Any], dict[str, Any]]] = {
    "wsgi": _environ_fields,
    "asgi": _scope_fields,
}


class RequestFilter:
    """The predicate filter_requests() returns; see that function.

    Not itself callable: the request modes recognise it and consult
    `matches()` with the fields of each request, and every other
    binding kind refuses it, since the fields it names exist only on
    a request.

    `matches()` is equally the supported door for evaluating the
    filter explicitly. A hand-written boundary the request modes do
    not speak for, a block() opened at a server's own seam say,
    computes the field values as its event records them, asks
    `matches()` for the decision, and hands the resulting bool to
    `block(when=..., tree=True)`, keeping the glob semantics
    identical to every when= that takes the filter directly.
    """

    __slots__ = ("_accept", "_ignore")

    def __init__(
        self,
        accept: Mapping[str, tuple[str, ...]],
        ignore: Mapping[str, tuple[str, ...]],
    ) -> None:
        self._accept = dict(accept)
        self._ignore = dict(ignore)

    def __repr__(self) -> str:
        parts = []
        if self._accept:
            parts.append(f"accept={self._accept!r}")
        if self._ignore:
            parts.append(f"ignore={self._ignore!r}")

        return f"filter_requests({', '.join(parts)})"

    @property
    def accept(self) -> dict[str, tuple[str, ...]]:
        """The accept table, each field's patterns as a tuple."""

        return dict(self._accept)

    @property
    def ignore(self) -> dict[str, tuple[str, ...]]:
        """The ignore table, each field's patterns as a tuple."""

        return dict(self._ignore)

    def matches(self, fields: Mapping[str, Any]) -> bool:
        """Whether a request with these field values is to be recorded.

        A request matching any ignore pattern is declined whatever
        the accept table says; otherwise every field the accept table
        names must match one of its patterns. A field that is not a
        string (absent, or not what the carrier promised) matches no
        pattern at all, so it passes ignore and fails accept.
        """

        for name, patterns in self._ignore.items():
            if _matches(name, fields.get(name), patterns):
                return False

        for name, patterns in self._accept.items():
            if not _matches(name, fields.get(name), patterns):
                return False

        return True


def _matches(name: str, value: Any, patterns: tuple[str, ...]) -> bool:
    # Only a string value can match: nothing is rendered to test it.
    # Methods compare case-insensitively, since the patterns were
    # upper-cased at construction.

    if not isinstance(value, str):
        return False

    if name == "method":
        value = value.upper()

    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _field_table(table: Any, side: str) -> dict[str, tuple[str, ...]]:
    # Validate one side of the filter: a mapping of request field to
    # one glob or a list of globs, each field named at most once and
    # nothing that can never match.

    if table is None:
        return {}

    if not isinstance(table, Mapping):
        raise TypeError(
            f"filter_requests(): {side} must be a mapping of request field to"
            f" glob pattern or list of patterns, got {table!r}"
        )

    if not table:
        raise ValueError(
            f"filter_requests(): {side} is empty; name at least one field or"
            f" leave it out"
        )

    result: dict[str, tuple[str, ...]] = {}

    for name, value in table.items():
        if name not in REQUEST_FIELDS:
            raise ValueError(
                f"filter_requests(): {side} names {name!r}, which is not a"
                f" request field; the fields are {', '.join(REQUEST_FIELDS)}"
            )

        if isinstance(value, str):
            patterns: tuple[Any, ...] = (value,)
        else:
            try:
                patterns = tuple(value)
            except TypeError:
                patterns = (value,)

        if not patterns or not all(isinstance(each, str) for each in patterns):
            raise TypeError(
                f"filter_requests(): {side}[{name!r}] must be a glob pattern or"
                f" a non-empty list of patterns, got {value!r}"
            )

        if name == "method":
            patterns = tuple(each.upper() for each in patterns)

        result[name] = patterns

    return result


def filter_requests(
    *,
    accept: FieldTable | None = None,
    ignore: FieldTable | None = None,
) -> RequestFilter:
    """A request predicate for when= on the request modes, declared as
    tables of request fields to glob patterns.

        application = WSGIMiddleware(
            application,
            when=filter_requests(ignore={"path": ["/health", "/static/*"]}),
            tree=True,
        )

    The fields are those the request event records: `method`, `path`,
    `scheme`, `protocol` and `remote`, valued exactly as the event
    would record them (for WSGI the path is SCRIPT_NAME plus
    PATH_INFO; for ASGI it is scope["path"]). Each field maps to one
    fnmatchcase glob or a list of them, any of which may match, so a
    list of plain strings reads as a set of exact values. Methods
    compare case-insensitively; everything else is exact in case.

    `ignore` names requests not to record: a request matching any
    pattern of any field it lists is declined. `accept` names the
    requests to record: every field it lists must match one of its
    patterns. Given both, a request must pass both, so ignore wins
    where they overlap; a field absent from a table is unconstrained.
    At least one table is required, an empty table is an error rather
    than a filter that can never act, and a field name that is not a
    request field is refused.

    The filter decides recording only: a declined request is served
    exactly as it would be otherwise. Pass it to `when=` on
    WSGIMiddleware, ASGIMiddleware or a mode="wsgi"/"asgi" binding,
    normally with `tree=True` so that nothing recorded beneath a
    declined request is left as a parentless root of its own. Every
    other binding kind refuses it, since the fields it names exist
    only on a request. An observe entry's `requests` table is this
    call spelt as TOML, with `tree=True` implied.

    Where the boundary is not a request mode at all, a hand-written
    block() at a server's own seam, evaluate the filter explicitly
    with `matches()`, passing the fields valued as the event records
    them, and hand the bool to `block(when=..., tree=True)`; see
    RequestFilter.
    """

    if accept is None and ignore is None:
        raise ValueError("filter_requests() needs accept=, ignore=, or both")

    return RequestFilter(_field_table(accept, "accept"), _field_table(ignore, "ignore"))


def _predicate_for(
    request_filter: RequestFilter, mode: str
) -> Callable[[Any, tuple[Any, ...], dict[str, Any]], bool]:
    """Adapt a RequestFilter to the binding-internal calling convention
    for one request mode: the carrier (environ or scope) arrives as the
    single positional argument."""

    fields_of = _FIELDS_OF[mode]

    def decide(instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        return request_filter.matches(fields_of(args[0]))

    return decide
