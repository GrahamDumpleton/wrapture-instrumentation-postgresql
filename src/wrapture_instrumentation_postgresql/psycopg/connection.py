"""The connection seams: connections being opened, and the
transaction boundaries the connection itself performs, recorded as
database events.

`Connection.connect` and `AsyncConnection.connect` are the
classmethods every connection comes from: the module-level
`psycopg.connect` is the same classmethod under another name, bound
at import, so it is bound as a module attribute too, as sqlite3's two
spellings of `connect` are; a pool (psycopg_pool) calls the class's
method, so pooled connections record through it. The connect event's
arguments are never captured (the conninfo carries a password); the
server it reached is annotated from the connection's info afterwards.

`commit()` and `rollback()` go to libpq directly rather than through
a cursor, so they are bound in their own right. The connection's
context manager exit commits, or rolls back when an exception is on
its way through, then closes the connection unless it belongs to a
pool; the exit is bound and records which of the two it performed.
The BEGIN psycopg issues implicitly before the first statement of a
transaction is sent inside that statement's execute, so it folds into
that event.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import SYSTEM, captured, server_of


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind connect, commit, rollback and the context manager exit on
    the sync and async connection classes; register their removal as
    this trigger's cleanup."""

    def opens(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, operation="CONNECT")

        connection = wrapped(*args, **kwargs)
        wrapture.annotate(**server_of(connection.info))

        return connection

    async def opens_async(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, operation="CONNECT")

        connection = await wrapped(*args, **kwargs)
        wrapture.annotate(**server_of(connection.info))

        return connection

    def performs(operation: str) -> Any:
        def record(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            wrapture.annotate(
                system=SYSTEM, operation=operation, **server_of(instance.info)
            )

            return wrapped(*args, **kwargs)

        return record

    def performs_async(operation: str) -> Any:
        async def record(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            wrapture.annotate(
                system=SYSTEM, operation=operation, **server_of(instance.info)
            )

            return await wrapped(*args, **kwargs)

        return record

    def exit_operation(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        # The exit commits unless an exception is on its way through.

        exc_type = args[0] if args else kwargs.get("exc_type")

        return "COMMIT" if exc_type is None else "ROLLBACK"

    def leaves(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(
            system=SYSTEM,
            operation=exit_operation(args, kwargs),
            **server_of(instance.info),
        )

        return wrapped(*args, **kwargs)

    async def leaves_async(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(
            system=SYSTEM,
            operation=exit_operation(args, kwargs),
            **server_of(instance.info),
        )

        return await wrapped(*args, **kwargs)

    def database_binding(
        owner: Any, name: str, capture_args: Any = captured
    ) -> wrapture.Binding:
        return wrapture.binding(
            owner,
            name,
            category="database",
            leaf=True,
            capture_args=capture_args,
            capture_result=captured,
        )

    named: dict[str, wrapture.Binding] = {}

    # The connect classmethods, and the module-level spelling of the
    # sync one; none captures its arguments.

    connect = database_binding(module.Connection, "connect", "none")
    connect.on_call.decorates(opens)
    named["connect"] = connect

    async_connect = database_binding(module.AsyncConnection, "connect", "none")
    async_connect.on_call.decorates(opens_async)
    named["async_connect"] = async_connect

    module_connect = database_binding(module, "connect", "none")
    module_connect.on_call.decorates(opens)
    named["module_connect"] = module_connect

    # The transaction boundaries the connection performs itself.

    for method, operation in (("commit", "COMMIT"), ("rollback", "ROLLBACK")):
        bound = database_binding(module.Connection, method)
        bound.on_call.decorates(performs(operation))
        named[method] = bound

        bound = database_binding(module.AsyncConnection, method)
        bound.on_call.decorates(performs_async(operation))
        named[f"async_{method}"] = bound

    closes = database_binding(module.Connection, "__exit__")
    closes.on_call.decorates(leaves)
    named["exit"] = closes

    async_closes = database_binding(module.AsyncConnection, "__aexit__")
    async_closes.on_call.decorates(leaves_async)
    named["async_exit"] = async_closes

    group = wrapture.bindings(**named)
    group.apply()

    instrumentation.on_cleanup(group.remove)
