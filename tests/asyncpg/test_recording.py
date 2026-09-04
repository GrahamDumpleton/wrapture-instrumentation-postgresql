"""What the instrumentation records: the connect, the execute and
fetch families, transactions through execute, prepared statements,
server-side cursors, COPY, a pooled connection, and what stays out
of capture."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytest.importorskip("asyncpg")

import asyncpg
from wrapture import Event, Tape, instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_postgresql.asyncpg import AsyncpgInstrumentation

EXECUTE = "asyncpg.connection:Connection.execute"
FETCH = "asyncpg.connection:Connection.fetch"


def at(tape: Tape, path: str) -> list[Event]:
    return [event for event in tape.all if event.path == path]


def recorded(tape: Tape) -> str:
    """Everything the tape holds, for asserting what never appears."""

    return repr(
        [
            (event.path, event.label, event.data, event.arguments, event.result)
            for event in tape.all
        ]
    )


def contract(event: Event, postgresql: Server, operation: str) -> None:
    assert event.category == "database"
    assert event.data["system"] == "postgresql"
    assert event.data["operation"] == operation
    assert event.data["database"] == postgresql.dbname
    assert event.data["host"] == postgresql.host
    assert event.data["port"] == postgresql.port


def run(
    postgresql: Server, body: Callable[[asyncpg.Connection], Awaitable[Any]]
) -> Any:
    """Open a connection, run the body against it, close it."""

    async def workload() -> Any:
        connection = await asyncpg.connect(postgresql.url)
        try:
            return await body(connection)
        finally:
            await connection.close()

    return asyncio.run(workload())


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_records_a_database_leaf(postgresql: Server, tape: Tape) -> None:
    run(postgresql, lambda connection: asyncio.sleep(0))

    (event,) = at(tape, "asyncpg:connect")
    contract(event, postgresql, "CONNECT")
    assert event.label is None
    assert event.arguments is None
    assert event.result == "<Connection>"
    assert tape.children_of(event) == []


def test_both_spellings_of_connect_record_without_their_arguments(
    postgresql: Server, tape: Tape
) -> None:
    # asyncpg spells the keyword form's database `database`, where the
    # fixture's kwargs follow libpq's `dbname`.

    kwargs = dict(postgresql.kwargs)
    kwargs["database"] = kwargs.pop("dbname")

    async def workload_kwargs() -> None:
        by_module = await asyncpg.connection.connect(**kwargs)
        await by_module.close()

    asyncio.run(workload_kwargs())

    async def workload_url() -> None:
        by_package = await asyncpg.connect(postgresql.url)
        await by_package.close()

    asyncio.run(workload_url())

    events = at(tape, "asyncpg:connect") + at(tape, "asyncpg.connection:connect")
    assert len(events) == 2
    assert all(event.arguments is None for event in events)
    assert f":{postgresql.password}@" not in recorded(tape)
    assert "password" not in recorded(tape)


# ---------------------------------------------------------------------------
# the execute and fetch families
# ---------------------------------------------------------------------------


def test_execute_records_operation_but_no_statement(
    postgresql: Server, tape: Tape
) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.execute("CREATE TEMP TABLE secrets (value TEXT)")
        await connection.execute("INSERT INTO secrets VALUES ($1)", "hunter2")

    run(postgresql, body)

    create, insert = at(tape, EXECUTE)
    contract(create, postgresql, "CREATE")
    contract(insert, postgresql, "INSERT")

    for event in (create, insert):
        assert "statement" not in event.data

    # The SQL reduces to its length and the arguments to a count in
    # the captured arguments, so the value never reaches the record.

    assert insert.arguments is not None
    assert insert.arguments["query"] == "<31 chars>"
    assert insert.arguments["args"] == "<1 values>"
    assert "hunter2" not in recorded(tape)


def test_the_statement_setting_records_the_text_as_handed_over(
    postgresql: Server,
) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.execute("CREATE TEMP TABLE items (name TEXT)")
        await connection.execute("INSERT INTO items VALUES ($1)", "widget")
        await connection.fetch("SELECT name FROM items")

    with instrumentation(AsyncpgInstrumentation, statement=True), timeline() as tape:
        run(postgresql, body)

    create, insert = at(tape, EXECUTE)
    assert create.data["statement"] == "CREATE TEMP TABLE items (name TEXT)"
    assert insert.data["statement"] == "INSERT INTO items VALUES ($1)"
    assert "widget" not in recorded(tape)

    (select,) = at(tape, FETCH)
    assert select.data["statement"] == "SELECT name FROM items"


def test_the_fetch_family_records(postgresql: Server, tape: Tape) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.execute("CREATE TEMP TABLE items (n INT)")
        await connection.executemany("INSERT INTO items VALUES ($1)", [(1,), (2,)])

        rows = await connection.fetch("SELECT n FROM items ORDER BY n")
        assert [row["n"] for row in rows] == [1, 2]
        assert (await connection.fetchrow("SELECT count(*) AS c FROM items"))["c"] == 2
        assert await connection.fetchval("SELECT max(n) FROM items") == 2

        if hasattr(connection, "fetchmany"):
            many = await connection.fetchmany("SELECT $1::int AS n", [(7,), (8,)])
            assert [row["n"] for row in many] == [7, 8]

    run(postgresql, body)

    (many,) = at(tape, "asyncpg.connection:Connection.executemany")
    contract(many, postgresql, "INSERT")
    assert many.arguments is not None
    assert many.arguments["command"] == "<29 chars>"
    assert many.arguments["args"] == "<2 values>"

    for path in (
        FETCH,
        "asyncpg.connection:Connection.fetchrow",
        "asyncpg.connection:Connection.fetchval",
    ):
        (event,) = at(tape, path)
        contract(event, postgresql, "SELECT")


def test_a_script_of_several_statements_records_its_first_keyword(
    postgresql: Server, tape: Tape
) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.execute(
            "CREATE TEMP TABLE items (n INT); INSERT INTO items VALUES (1);"
        )

    run(postgresql, body)

    (event,) = at(tape, EXECUTE)
    contract(event, postgresql, "CREATE")


def test_a_failing_query_records_its_exception(postgresql: Server, tape: Tape) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.fetch("SELECT nope FROM nowhere")

    with pytest.raises(asyncpg.UndefinedTableError):
        run(postgresql, body)

    (event,) = at(tape, FETCH)
    contract(event, postgresql, "SELECT")
    assert isinstance(event.exception, asyncpg.UndefinedTableError)


# ---------------------------------------------------------------------------
# transactions, through execute
# ---------------------------------------------------------------------------


def test_transactions_record_through_execute(postgresql: Server, tape: Tape) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.execute("CREATE TEMP TABLE items (name TEXT)")

        async with connection.transaction():
            await connection.execute("INSERT INTO items VALUES ('outer')")

            async with connection.transaction():
                await connection.execute("INSERT INTO items VALUES ('released')")

            with pytest.raises(RuntimeError):
                async with connection.transaction():
                    await connection.execute("INSERT INTO items VALUES ('undone')")
                    raise RuntimeError("undo")

        rows = await connection.fetch("SELECT name FROM items ORDER BY name")
        assert [row["name"] for row in rows] == ["outer", "released"]

    run(postgresql, body)

    # asyncpg issues every boundary as a statement through execute, so
    # the boundaries are ordinary execute events with their own
    # operations, terminators stripped.

    assert [event.data["operation"] for event in at(tape, EXECUTE)] == [
        "CREATE",
        "BEGIN",
        "INSERT",
        "SAVEPOINT",
        "INSERT",
        "RELEASE",
        "SAVEPOINT",
        "INSERT",
        "ROLLBACK",
        "COMMIT",
    ]


# ---------------------------------------------------------------------------
# prepared statements, server-side cursors and COPY
# ---------------------------------------------------------------------------


def test_a_prepared_statement_records_its_own_fetches(
    postgresql: Server,
) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.execute("CREATE TEMP TABLE items (n INT)")
        await connection.execute("INSERT INTO items VALUES (1), (2), (3)")

        statement = await connection.prepare("SELECT n FROM items WHERE n > $1")
        assert await statement.fetchval(2) == 3
        assert len(await statement.fetch(1)) == 2
        assert (await statement.fetchrow(2))["n"] == 3
        await statement.explain(0)

    with instrumentation(AsyncpgInstrumentation, statement=True), timeline() as tape:
        run(postgresql, body)

    for method in ("fetchval", "fetch", "fetchrow"):
        (event,) = at(tape, f"asyncpg.prepared_stmt:PreparedStatement.{method}")
        contract(event, postgresql, "SELECT")
        assert event.data["statement"] == "SELECT n FROM items WHERE n > $1"
        assert event.arguments is not None
        assert event.arguments["args"] == "<1 values>"

    (explain,) = at(tape, "asyncpg.prepared_stmt:PreparedStatement.explain")
    contract(explain, postgresql, "EXPLAIN")

    # The prepare itself is not a query and records nothing.

    assert at(tape, "asyncpg.connection:Connection.prepare") == []


def test_a_server_side_cursor_records_each_round_trip(
    postgresql: Server,
) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.execute("CREATE TEMP TABLE items (n INT)")
        await connection.execute("INSERT INTO items VALUES (1), (2), (3), (4), (5)")

        async with connection.transaction():
            iterated = [
                row["n"]
                async for row in connection.cursor(
                    "SELECT n FROM items ORDER BY n", prefetch=2
                )
            ]
            assert iterated == [1, 2, 3, 4, 5]

            cursor = await connection.cursor("SELECT n FROM items ORDER BY n")
            assert [row["n"] for row in await cursor.fetch(2)] == [1, 2]
            assert await cursor.forward(1) == 1
            assert (await cursor.fetchrow())["n"] == 4

    with instrumentation(AsyncpgInstrumentation, statement=True), timeline() as tape:
        run(postgresql, body)

    # The iteration: one DECLARE with the first batch of two, then a
    # FETCH per batch until a short batch says the rows have run out.
    # The hand-driven cursor: a DECLARE alone, a FETCH for the fetch
    # and for the fetchrow, and a MOVE for the forward.

    declares = at(tape, "asyncpg.cursor:BaseCursor._bind_exec") + at(
        tape, "asyncpg.cursor:BaseCursor._bind"
    )
    assert len(declares) == 2
    for event in declares:
        contract(event, postgresql, "DECLARE")
        assert event.data["statement"] == "SELECT n FROM items ORDER BY n"

    fetches = at(tape, "asyncpg.cursor:BaseCursor._exec")
    assert len(fetches) == 2 + 2
    for event in fetches:
        contract(event, postgresql, "FETCH")

    (move,) = at(tape, "asyncpg.cursor:Cursor.forward")
    contract(move, postgresql, "MOVE")


def test_copy_records_the_table_or_the_query(postgresql: Server, tape: Tape) -> None:
    async def body(connection: asyncpg.Connection) -> None:
        await connection.execute("CREATE TEMP TABLE items (n INT)")
        await connection.copy_records_to_table("items", records=[(1,), (2,)])

        source = io.BytesIO(b"3\n4\n")
        await connection.copy_to_table("items", source=source)

        output = io.BytesIO()
        await connection.copy_from_table("items", output=output)
        assert output.getvalue() == b"1\n2\n3\n4\n"

        output = io.BytesIO()
        await connection.copy_from_query(
            "SELECT n FROM items WHERE n > $1", 2, output=output
        )
        assert output.getvalue() == b"3\n4\n"

    run(postgresql, body)

    for method in ("copy_records_to_table", "copy_to_table", "copy_from_table"):
        (event,) = at(tape, f"asyncpg.connection:Connection.{method}")
        contract(event, postgresql, "COPY")
        assert event.data["collection"] == "items"

    (from_query,) = at(tape, "asyncpg.connection:Connection.copy_from_query")
    contract(from_query, postgresql, "COPY")
    assert "collection" not in from_query.data
    assert from_query.arguments is not None
    assert from_query.arguments["query"] == "<32 chars>"
    assert from_query.arguments["args"] == "<1 values>"
    assert from_query.arguments["output"] == "<BytesIO>"

    (to_table,) = at(tape, "asyncpg.connection:Connection.copy_to_table")
    assert to_table.arguments is not None
    assert to_table.arguments["source"] == "<BytesIO>"
    (records,) = at(tape, "asyncpg.connection:Connection.copy_records_to_table")
    assert records.arguments is not None
    assert records.arguments["records"] == "<2 values>"


# ---------------------------------------------------------------------------
# pools
# ---------------------------------------------------------------------------


def test_a_pool_opens_its_connections_through_connect(
    postgresql: Server, tape: Tape
) -> None:
    async def workload() -> None:
        pool = await asyncpg.create_pool(postgresql.url, min_size=1, max_size=1)
        await pool.close()

    asyncio.run(workload())

    # The pool looks connection.connect up when it is made, and awaits
    # it on the caller's task, so the open records.

    (event,) = at(tape, "asyncpg.connection:connect")
    contract(event, postgresql, "CONNECT")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "asyncpg's PoolConnectionProxy calls Connection's methods through the"
        " class, Connection.fetchval(connection, query), and wrapture's"
        " signature check binds the arguments against the unbound function,"
        " so the call is refused as missing its query; a wrapture fix is"
        " needed (resolve the signature with the instance already bound"
        " when wrapt hands over a partial)"
    ),
)
def test_a_pooled_connection_records_its_queries(
    postgresql: Server, tape: Tape
) -> None:
    async def workload() -> int:
        pool = await asyncpg.create_pool(postgresql.url, min_size=1, max_size=1)
        try:
            async with pool.acquire() as connection:
                value: int = await connection.fetchval("SELECT 42")
                return value
        finally:
            await pool.close()

    assert asyncio.run(workload()) == 42

    (event,) = at(tape, "asyncpg.connection:Connection.fetchval")
    contract(event, postgresql, "SELECT")
