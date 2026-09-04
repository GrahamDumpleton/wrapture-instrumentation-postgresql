"""What every PostgreSQL driver's events have in common: the database
category's contract keys, the operation name derived from the SQL,
and the capture policy that keeps queries and their data out of the
record.

This module imports only wrapture, so a target subpackage can import
it at load time without dragging any driver in.

Every event carries `system` ("postgresql") and `operation` (the SQL's
leading keyword, or CONNECT, COMMIT, ROLLBACK and their kin), plus
`database`, `host` and `port` read from the driver's connection info,
so each event says which server it went to. The SQL text is recorded
as `statement` only when the target's `statement` setting is on, as
the application handed it to the driver and never with its bound
parameters, which no setting captures.
"""

from __future__ import annotations

from typing import Any

SYSTEM = "postgresql"

# The argument names under which the drivers take SQL text, and those
# under which they take its parameters; the capture policy reduces
# the first to a length and the second to a count.

_QUERY_NAMES = frozenset({"query", "sql", "statement", "command"})
_PARAMETER_NAMES = frozenset(
    {"params", "params_seq", "vars", "vars_list", "parameters", "args", "records"}
)

# File-like arguments (a COPY's source or sink) reduce to their type:
# their repr can name a path, and their contents are the data.

_FILE_NAMES = frozenset({"file", "source", "output"})


def operation_of(sql: str) -> str:
    """The SQL's leading keyword, uppercased: the low-cardinality
    operation name the database contract carries."""

    head = sql.split(None, 1)

    # A statement may end in its keyword ("COMMIT;"): the terminator is
    # not part of the operation.

    return head[0].upper().rstrip(";") if head else "?"


def statement_of(query: Any, context: Any = None) -> str | None:
    """The SQL text of a query as the driver was handed it: a string
    as is, bytes decoded, a composed query (psycopg's `sql.SQL` and
    kin) rendered against the cursor or connection it will run on,
    anything else None."""

    if isinstance(query, str):
        return query

    if isinstance(query, (bytes, bytearray, memoryview)):
        try:
            return bytes(query).decode()
        except UnicodeDecodeError:
            return None

    render = getattr(query, "as_string", None)
    if callable(render):
        try:
            rendered = render(context)
        except Exception:
            return None
        return rendered if isinstance(rendered, str) else None

    return None


def server_of(info: Any) -> dict[str, Any]:
    """The `database`, `host` and `port` keys from a driver's
    connection info object (psycopg's and psycopg2's both expose
    `dbname`, `host` and `port`), whichever of them it can supply."""

    data: dict[str, Any] = {}

    for key, attribute in (("database", "dbname"), ("host", "host"), ("port", "port")):
        try:
            value = getattr(info, attribute)
        except Exception:
            continue

        if value not in (None, ""):
            data[key] = value

    return data


def statement_data(
    query: Any,
    context: Any,
    server: dict[str, Any],
    record_statement: bool,
    operation: str | None = None,
) -> dict[str, Any]:
    """The data for one query event: the contract keys, the server
    keys given, and the statement text when the setting asks for it.
    The operation is the one given, else the query's leading keyword."""

    text = statement_of(query, context)

    data: dict[str, Any] = {"system": SYSTEM}

    if operation is not None:
        data["operation"] = operation
    elif text is not None:
        data["operation"] = operation_of(text)

    data.update(server)

    if record_statement and text is not None:
        data["statement"] = text

    return data


def query_data(
    query: Any,
    context: Any,
    info: Any,
    record_statement: bool,
    operation: str | None = None,
) -> dict[str, Any]:
    """statement_data() with the server keys read from a driver's
    connection info object (psycopg's and psycopg2's)."""

    return statement_data(query, context, server_of(info), record_statement, operation)


def captured(name: str | None, value: Any) -> Any:
    """SQL text reduces to its length, parameters to a count or their
    type (a parameter sequence may be a generator the driver has yet
    to consume, and is never iterated), a COPY's file to its type, a
    context manager exit's exception value and traceback to their
    types, and every unnamed value to its type: the query and its
    data never reach the record through argument capture."""

    if name in _QUERY_NAMES:
        text = statement_of(value)
        if text is not None:
            return f"<{len(text)} chars>"
        return f"<{type(value).__name__}>"

    if name in _PARAMETER_NAMES:
        if isinstance(value, (list, tuple, dict)):
            return f"<{len(value)} values>"
        return f"<{type(value).__name__}>"

    if name in _FILE_NAMES:
        return f"<{type(value).__name__}>"

    # An exception's message is application data like any other; the
    # exit event's exception, when one escapes, is recorded properly
    # on the event itself.

    if name in ("exc_value", "exc_val", "traceback", "exc_tb") and value is not None:
        return f"<{type(value).__name__}>"

    if name is None:
        return f"<{type(value).__name__}>"

    return value
