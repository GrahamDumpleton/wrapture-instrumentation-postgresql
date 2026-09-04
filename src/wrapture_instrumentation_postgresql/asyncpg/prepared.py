"""The prepared statement seams: a PreparedStatement's own fetch and
execute family, recorded as database events around their awaits.

`await connection.prepare(query)` hands back a PreparedStatement whose
`fetch`, `fetchrow`, `fetchval`, `fetchmany`, `executemany` and
`explain` bypass Connection's public methods, driving the protocol
through the statement's own private path, so each is bound in its
own right. The statement's SQL is public (`get_query()`), and the
connection it belongs to supplies the server keys. The `prepare` call
itself is not a query and is not recorded; the round trip that
prepares happens inside it.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import statement_data
from .connection import database_binding, server_of


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind the prepared statement's methods; register their removal
    as this trigger's cleanup."""

    settings = instrumentation.settings
    record_statement = bool(settings["statement"])

    def executes(operation: str | None = None) -> Any:
        def decorator(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            async def record() -> Any:
                connection = getattr(instance, "_connection", None)
                query = instance.get_query()
                wrapture.annotate(
                    **statement_data(
                        query,
                        connection,
                        server_of(connection),
                        record_statement,
                        operation,
                    )
                )

                return await wrapped(*args, **kwargs)

            return record()

        return decorator

    statement_class = module.PreparedStatement
    named: dict[str, wrapture.Binding] = {}

    for method in ("fetch", "fetchrow", "fetchval", "fetchmany", "executemany"):
        if method not in vars(statement_class):
            continue
        bound = database_binding(statement_class, method)
        bound.on_call.decorates(executes())
        named[method] = bound

    explain = database_binding(statement_class, "explain")
    explain.on_call.decorates(executes("EXPLAIN"))
    named["explain"] = explain

    group = wrapture.bindings(**named)
    group.apply()

    instrumentation.on_cleanup(group.remove)
