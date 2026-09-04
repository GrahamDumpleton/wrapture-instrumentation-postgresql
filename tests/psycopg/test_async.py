"""The async twins: AsyncConnection, AsyncCursor and AsyncTransaction
record the same events as the sync classes, around the awaits."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("psycopg")

import psycopg
from wrapture import Event, Tape, instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_postgresql.psycopg import PsycopgInstrumentation


def at(tape: Tape, path: str) -> list[Event]:
    return [event for event in tape.all if event.path == path]


def test_the_async_classes_record_the_same_shapes(
    postgresql: Server, tape: Tape
) -> None:
    async def workload() -> list[tuple[str]]:
        async with await psycopg.AsyncConnection.connect(postgresql.url) as conn:
            await conn.execute("CREATE TEMP TABLE items (name TEXT)")

            async with conn.cursor() as cursor:
                await cursor.execute("INSERT INTO items VALUES (%s)", ("a",))
                await cursor.executemany(
                    "INSERT INTO items VALUES (%s)", [("b",), ("c",)]
                )

                async with cursor.copy("COPY items (name) FROM STDIN") as copy:
                    await copy.write_row(("d",))

                streamed = [
                    row async for row in cursor.stream("SELECT name FROM items")
                ]

            async with conn.transaction():
                await conn.execute("INSERT INTO items VALUES ('e')")

            async with conn.cursor(name="items") as server_cursor:
                await server_cursor.execute("SELECT name FROM items")
                await server_cursor.fetchall()

            await conn.commit()

            return streamed

    streamed = asyncio.run(workload())
    assert len(streamed) == 4

    (connect,) = at(tape, "psycopg:AsyncConnection.connect")
    assert connect.data["operation"] == "CONNECT"
    assert connect.data["database"] == postgresql.dbname
    assert connect.arguments is None

    executes = at(tape, "psycopg:AsyncCursor.execute")
    assert [event.data["operation"] for event in executes] == [
        "CREATE",
        "INSERT",
        "INSERT",
    ]
    (many,) = at(tape, "psycopg:AsyncCursor.executemany")
    assert many.arguments is not None
    assert many.arguments["params_seq"] == "<2 values>"

    (copy_block,) = [event for event in tape.all if event.kind == "block"]
    assert copy_block.label == "psycopg:AsyncCursor.copy"
    assert copy_block.data["operation"] == "COPY"
    assert copy_block.data["rows"] == 1

    (stream,) = at(tape, "psycopg:AsyncCursor.stream")
    assert stream.data["operation"] == "SELECT"
    assert stream.items == 4

    (enter,) = at(tape, "psycopg:AsyncTransaction.__aenter__")
    assert enter.data["operation"] == "SAVEPOINT"
    (exit_event,) = at(tape, "psycopg:AsyncTransaction.__aexit__")
    assert exit_event.data["operation"] == "RELEASE"

    (declare,) = at(tape, "psycopg:AsyncServerCursor.execute")
    assert declare.data["operation"] == "DECLARE"

    (commit,) = at(tape, "psycopg:AsyncConnection.commit")
    assert commit.data["operation"] == "COMMIT"
    (closing,) = at(tape, "psycopg:AsyncConnection.__aexit__")
    assert closing.data["operation"] == "COMMIT"

    for event in tape.all:
        assert event.category == "database"
        assert event.data["system"] == "postgresql"
        assert event.data["host"] == postgresql.host


def test_an_async_failure_records_its_exception_and_rolls_back(
    postgresql: Server, tape: Tape
) -> None:
    async def workload() -> None:
        async with await psycopg.AsyncConnection.connect(postgresql.url) as conn:
            await conn.execute("SELECT nope FROM nowhere")

    with pytest.raises(psycopg.errors.UndefinedTable):
        asyncio.run(workload())

    (execute,) = at(tape, "psycopg:AsyncCursor.execute")
    assert isinstance(execute.exception, psycopg.errors.UndefinedTable)
    (closing,) = at(tape, "psycopg:AsyncConnection.__aexit__")
    assert closing.data["operation"] == "ROLLBACK"


def test_the_statement_setting_applies_to_the_async_classes(
    postgresql: Server,
) -> None:
    async def workload() -> None:
        async with await psycopg.AsyncConnection.connect(postgresql.url) as conn:
            await conn.execute("SELECT %s::int", (1,))

    with instrumentation(PsycopgInstrumentation, statement=True), timeline() as tape:
        asyncio.run(workload())

    (execute,) = at(tape, "psycopg:AsyncCursor.execute")
    assert execute.data["statement"] == "SELECT %s::int"
