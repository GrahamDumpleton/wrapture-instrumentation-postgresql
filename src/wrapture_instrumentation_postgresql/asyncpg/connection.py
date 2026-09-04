"""The connection seams: connections being opened, and Connection's
execute, fetch and COPY families, recorded as database events around
their awaits.

asyncpg's public surface is pure Python coroutine methods on
`Connection`, so each is bound in place on the class and records
around its await: `execute` (with or without arguments, a script of
several statements included), `executemany`, `fetch`, `fetchrow`,
`fetchval`, `fetchmany` (0.30 and later), and the four COPY methods.
The Cython protocol beneath them does the socket work and is where
the seams stop. A `PoolConnectionProxy` forwards each of these to the
real Connection's class attribute, so pooled connections record
through the same bindings.

`asyncpg.connection.connect` is the coroutine function every
connection comes from; `asyncpg.connect` is the same function under
the package's name, bound at import, so it is bound as a module
attribute too, as sqlite3's two spellings of `connect` are. A pool
looks `connection.connect` up when it is created, so a pool made after
the instrumentation is applied opens its connections through the
binding, on the task that awaits it. The connect event's arguments are
never captured (the dsn and the keyword form carry the password); the
server reached is annotated from the connection afterwards.

Transactions need no bindings of their own: `Transaction.start()`,
its commit and its rollback issue BEGIN, COMMIT, ROLLBACK, SAVEPOINT,
RELEASE SAVEPOINT and ROLLBACK TO through `Connection.execute`, so
each boundary records as a statement event with its own operation.

Every event carries the database contract keys and the server the
connection reached, which asyncpg keeps privately (`_params.database`
and `_addr`, a host and port pair or a socket path, stable since its
0.1x line) and which are read through a guarded helper. The SQL text
rides as `statement` only when the setting is on; the query arguments
(asyncpg's `*args`, sent server-side) are never recorded and reduce
to a count.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import SYSTEM, captured, statement_data


def server_of(connection: Any) -> dict[str, Any]:
    """The `database`, `host` and `port` keys for an asyncpg
    connection, from what it keeps privately; whichever it can
    supply."""

    data: dict[str, Any] = {}

    params = getattr(connection, "_params", None)
    database = getattr(params, "database", None)
    if isinstance(database, str) and database:
        data["database"] = database

    addr = getattr(connection, "_addr", None)
    if isinstance(addr, tuple) and len(addr) == 2:
        host, port = addr
        if isinstance(host, str):
            data["host"] = host
        if isinstance(port, int):
            data["port"] = port
    elif isinstance(addr, str) and addr:
        data["host"] = addr

    return data


def query_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """The query a Connection method was handed: the first positional,
    else the keyword the method spells it as."""

    if args:
        return args[0]

    return kwargs.get("query", kwargs.get("command"))


def database_binding(
    owner: Any, name: str, capture_args: Any = captured
) -> wrapture.Binding:
    """A ready binding on one of the driver's methods, shared with the
    prepared statement and cursor modules."""

    return wrapture.binding(
        owner,
        name,
        category="database",
        leaf=True,
        capture_args=capture_args,
        capture_result=captured,
    )


def opens_binding(owner: Any) -> wrapture.Binding:
    """The connect binding for either spelling of connect."""

    async def opens(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, operation="CONNECT")

        connection = await wrapped(*args, **kwargs)
        wrapture.annotate(**server_of(connection))

        return connection

    binding = database_binding(owner, "connect", "none")
    binding.on_call.decorates(opens)

    return binding


def instrument_package(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind the package's own spelling of connect; register its
    removal as this trigger's cleanup."""

    group = wrapture.bindings(connect=opens_binding(module))
    group.apply()

    instrumentation.on_cleanup(group.remove)


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind connect and the Connection methods; register their removal
    as this trigger's cleanup."""

    settings = instrumentation.settings
    record_statement = bool(settings["statement"])

    def data_for(
        connection: Any, query: Any, operation: str | None = None
    ) -> dict[str, Any]:
        return statement_data(
            query, connection, server_of(connection), record_statement, operation
        )

    def queries(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        async def record() -> Any:
            wrapture.annotate(**data_for(instance, query_of(args, kwargs)))

            return await wrapped(*args, **kwargs)

        return record()

    def copies_table(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        async def record() -> Any:
            table = args[0] if args else kwargs.get("table_name")
            data = data_for(instance, None, "COPY")
            if isinstance(table, str):
                data["collection"] = table
            wrapture.annotate(**data)

            return await wrapped(*args, **kwargs)

        return record()

    def copies_query(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        async def record() -> Any:
            wrapture.annotate(**data_for(instance, query_of(args, kwargs), "COPY"))
            return await wrapped(*args, **kwargs)

        return record()

    named: dict[str, wrapture.Binding] = {"connect": opens_binding(module)}

    connection_class = module.Connection

    # The execute and fetch family; fetchmany arrived in 0.30.

    for method in (
        "execute",
        "executemany",
        "fetch",
        "fetchrow",
        "fetchval",
        "fetchmany",
    ):
        if method not in vars(connection_class):
            continue
        bound = database_binding(connection_class, method)
        bound.on_call.decorates(queries)
        named[method] = bound

    # COPY: three methods name a table, one takes a query.

    for method in ("copy_from_table", "copy_to_table", "copy_records_to_table"):
        bound = database_binding(connection_class, method)
        bound.on_call.decorates(copies_table)
        named[method] = bound

    bound = database_binding(connection_class, "copy_from_query")
    bound.on_call.decorates(copies_query)
    named["copy_from_query"] = bound

    group = wrapture.bindings(**named)
    group.apply()

    instrumentation.on_cleanup(group.remove)
