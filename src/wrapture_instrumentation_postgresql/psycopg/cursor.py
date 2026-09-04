"""The cursor seams: the execute family, streamed queries and COPY,
on the sync and async cursors and on the server-side cursors,
recorded as database events.

psycopg's cursors are pure Python, so each method is bound in place
on its class. `Cursor.execute` and `Cursor.executemany` are the doors
every ordinary query passes through, whichever cursor class the
application chose: `ClientCursor` and `RawCursor` inherit them, and
`Connection.execute`, the shortcut, builds a cursor and calls its
`execute` in Python, so the one binding records it too with no
double. The server-side cursors override `execute` to DECLARE a
cursor rather than run the query, so those overrides are bound in
their own right and record a DECLARE operation; their fetches, which
FETCH from the portal, are not recorded, the model every database
target here follows (a query event closes when its execute returns,
and time spent iterating rows is the application's).

`Cursor.stream()` is a generator: the query is sent on the first
iteration and rows arrive one at a time. The binding's decorator is
itself a generator over the driver's, so wrapture records the event
around the iteration, its duration the time spent inside the
generator and its item count the rows streamed.

`Cursor.copy()` is a context manager factory: the COPY statement is
sent on entering and the data flows through the yielded Copy object
until exit. A plain binding would record the factory call, an
instant, so the binding records nothing itself (`when=False`) and its
decorator hands back a wrapping context manager that opens a block
event on entry and closes it on exit, the block spanning the
transfer, with the row count annotated at the end.

Every event carries the database contract keys from the common
module; the SQL text rides as `statement` only when the setting is
on. Bound parameters are never recorded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import TracebackType
from typing import Any

import wrapture

from ..common import SYSTEM, captured, query_data, server_of


class RecordedCopy:
    """The context manager handed back in place of psycopg's copy
    factory result: a block event around the real one."""

    def __init__(
        self, label: str, cursor: Any, inner: Any, data: dict[str, Any]
    ) -> None:
        self._cursor = cursor
        self._inner = inner
        self._block = wrapture.block(label, category="database", data=data, leaf=True)

    def _finish(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # The cursor's rowcount is fresh once the inner exit has run
        # the rest of the factory's body, which reads the COPY result.

        rows = getattr(self._cursor, "rowcount", -1)
        if isinstance(rows, int) and rows >= 0:
            wrapture.annotate(rows=rows)

        self._block.__exit__(exc_type, exc_value, traceback)

    def __enter__(self) -> Any:
        self._block.__enter__()
        try:
            return self._inner.__enter__()
        except BaseException as error:
            self._block.__exit__(type(error), error, error.__traceback__)
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        try:
            outcome = self._inner.__exit__(exc_type, exc_value, traceback)
        except BaseException as error:
            self._finish(type(error), error, error.__traceback__)
            raise

        self._finish(exc_type, exc_value, traceback)

        return outcome

    async def __aenter__(self) -> Any:
        self._block.__enter__()
        try:
            return await self._inner.__aenter__()
        except BaseException as error:
            self._block.__exit__(type(error), error, error.__traceback__)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        try:
            outcome = await self._inner.__aexit__(exc_type, exc_value, traceback)
        except BaseException as error:
            self._finish(type(error), error, error.__traceback__)
            raise

        self._finish(exc_type, exc_value, traceback)

        return outcome


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind the execute family, stream and copy on the cursor classes;
    register their removal as this trigger's cleanup."""

    settings = instrumentation.settings
    record_statement = bool(settings["statement"])

    def query_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        return args[0] if args else kwargs.get("query", kwargs.get("statement"))

    def data_for(
        instance: Any, query: Any, operation: str | None = None
    ) -> dict[str, Any]:
        return query_data(
            query, instance, instance.connection.info, record_statement, operation
        )

    def executes(operation: str | None = None) -> Any:
        def record(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            wrapture.annotate(**data_for(instance, query_of(args, kwargs), operation))

            return wrapped(*args, **kwargs)

        return record

    def executes_async(operation: str | None = None) -> Any:
        async def record(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            wrapture.annotate(**data_for(instance, query_of(args, kwargs), operation))

            return await wrapped(*args, **kwargs)

        return record

    # The stream decorators are generators over the driver's, so the
    # annotation lands inside the event wrapture records around the
    # iteration, not before it exists.

    def streams(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Iterator[Any]:
        wrapture.annotate(**data_for(instance, query_of(args, kwargs)))

        yield from wrapped(*args, **kwargs)

    async def streams_async(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> AsyncIterator[Any]:
        wrapture.annotate(**data_for(instance, query_of(args, kwargs)))

        async for row in wrapped(*args, **kwargs):
            yield row

    def copies(label: str) -> Any:
        def record(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> RecordedCopy:
            data = data_for(instance, query_of(args, kwargs), "COPY")

            return RecordedCopy(label, instance, wrapped(*args, **kwargs), data)

        return record

    def query_binding(owner: Any, name: str) -> wrapture.Binding:
        return wrapture.binding(
            owner,
            name,
            category="database",
            leaf=True,
            capture_args=captured,
            capture_result=captured,
        )

    named: dict[str, wrapture.Binding] = {}

    # The execute family on the ordinary cursors, sync and async.

    for prefix, owner, decorator in (
        ("cursor", module.Cursor, executes),
        ("async_cursor", module.AsyncCursor, executes_async),
    ):
        for method in ("execute", "executemany"):
            bound = query_binding(owner, method)
            bound.on_call.decorates(decorator())
            named[f"{prefix}_{method}"] = bound

    # The server-side cursors' own execute, a DECLARE.

    for prefix, owner, decorator in (
        ("server_cursor", module.ServerCursor, executes),
        ("async_server_cursor", module.AsyncServerCursor, executes_async),
    ):
        bound = query_binding(owner, "execute")
        bound.on_call.decorates(decorator("DECLARE"))
        named[f"{prefix}_execute"] = bound

    # Streamed queries, recorded around the iteration.

    stream = query_binding(module.Cursor, "stream")
    stream.on_call.decorates(streams)
    named["cursor_stream"] = stream

    async_stream = query_binding(module.AsyncCursor, "stream")
    async_stream.on_call.decorates(streams_async)
    named["async_cursor_stream"] = async_stream

    # COPY: the factory records nothing itself, the wrapping context
    # manager its decorator returns records the block.

    for prefix, owner in (
        ("cursor", module.Cursor),
        ("async_cursor", module.AsyncCursor),
    ):
        bound = wrapture.binding(owner, "copy", when=False)
        bound.on_call.decorates(copies(f"psycopg:{owner.__name__}.copy"))
        named[f"{prefix}_copy"] = bound

    group = wrapture.bindings(**named)
    group.apply()

    instrumentation.on_cleanup(group.remove)


__all__ = ["RecordedCopy", "SYSTEM", "instrument", "server_of"]
