"""The server-side cursor seams: each round trip a cursor makes to
its portal, recorded as a database event around its await.

`connection.cursor(query, *args)` inside a transaction gives a
server-side cursor, iterated with `async for` or driven by `fetch`,
`fetchrow` and `forward`. Nothing there passes through Connection's
public methods: the DECLARE happens inside the cursor's own bind, and
every FETCH inside its exec. With no binding at all a query run this
way would leave no event, so the three private methods on
`BaseCursor` that make the round trips are bound: `_bind_exec` (the
DECLARE with the first batch, what `async for` does), `_bind` (the
DECLARE alone, for a cursor awaited then driven by hand) and `_exec`
(each FETCH). One event per round trip, whichever public method drove
it; binding `__anext__` instead would make an event per buffered row,
fifty times over. sqlalchemy's `_commit_impl` is the precedent for a
private seam where the public ones do not bound the work. `forward`,
which skips rows with a MOVE of its own rather than through `_exec`,
is bound as the public method it is.

The cursor knows its query (`_query`) and its connection, which
supply the statement (when the setting is on) and the server keys.
The operation is DECLARE for the two binds, FETCH for the exec and
MOVE for a forward.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import statement_data
from .connection import database_binding, server_of


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind the cursor's round trips; register their removal as this
    trigger's cleanup."""

    settings = instrumentation.settings
    record_statement = bool(settings["statement"])

    def round_trips(operation: str) -> Any:
        def decorator(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            async def record() -> Any:
                connection = getattr(instance, "_connection", None)
                query = getattr(instance, "_query", None)
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

    cursor_class = module.BaseCursor
    named: dict[str, wrapture.Binding] = {}

    for method, operation in (
        ("_bind_exec", "DECLARE"),
        ("_bind", "DECLARE"),
        ("_exec", "FETCH"),
    ):
        bound = database_binding(cursor_class, method)
        bound.on_call.decorates(round_trips(operation))
        named[method.lstrip("_")] = bound

    forward = database_binding(module.Cursor, "forward")
    forward.on_call.decorates(round_trips("MOVE"))
    named["forward"] = forward

    group = wrapture.bindings(**named)
    group.apply()

    instrumentation.on_cleanup(group.remove)
