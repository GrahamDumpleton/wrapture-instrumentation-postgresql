"""With the core package's sqlalchemy instrumentation applied
alongside, over the asyncpg dialect of the async engine: the default
leaf keeps the driver's events out of the tree, leaf off nests them
beneath each statement, and raw driver use beside the engine still
records."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy")
pytest.importorskip("wrapture_instrumentation")

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from wrapture import Event, Tape, instrumentation, timeline
from wrapture_instrumentation.database.sqlalchemy import SQLAlchemyInstrumentation

from tests.conftest import Server
from wrapture_instrumentation_postgresql.asyncpg import AsyncpgInstrumentation

EXECUTE = "sqlalchemy.engine.default:DefaultDialect.do_execute"
CONNECT = "sqlalchemy.engine.default:DefaultDialect.connect"
COMMIT = "sqlalchemy.engine.base:Connection._commit_impl"

# The statements as SQLAlchemy is handed them, and as the driver then
# sees them (a named parameter compiles to asyncpg's numbered form).

WORKLOAD = [
    ("CREATE TEMP TABLE items (name TEXT)", "CREATE TEMP TABLE items (name TEXT)"),
    ("INSERT INTO items VALUES (:name)", "INSERT INTO items VALUES ($1)"),
    ("SELECT name FROM items", "SELECT name FROM items"),
]
DRIVER_STATEMENTS = [driver for _, driver in WORKLOAD]


def at(tape: Tape, path: str) -> list[Event]:
    return [event for event in tape.all if event.path == path]


def driver_events(tape: Tape) -> list[Event]:
    """The asyncpg events that belong to the workload, leaving out the
    dialect's own setup queries, which run outside SQLAlchemy's
    seams."""

    return [
        event
        for event in tape.all
        if event.path.startswith("asyncpg")
        and event.data.get("statement") in DRIVER_STATEMENTS
    ]


def engine_url(postgresql: Server) -> str:
    return postgresql.url.replace("postgresql://", "postgresql+asyncpg://")


def workload(postgresql: Server) -> None:
    async def run() -> None:
        engine = create_async_engine(engine_url(postgresql))

        async with engine.begin() as connection:
            await connection.execute(text(WORKLOAD[0][0]))
            await connection.execute(text(WORKLOAD[1][0]), {"name": "a"})
            (await connection.execute(text(WORKLOAD[2][0]))).fetchall()

        await engine.dispose()

    asyncio.run(run())


def test_the_default_leaf_keeps_the_driver_out(postgresql: Server) -> None:
    with (
        instrumentation(AsyncpgInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        workload(postgresql)

    assert len(at(tape, EXECUTE)) >= 3
    assert len(at(tape, CONNECT)) == 1
    assert len(at(tape, COMMIT)) == 1

    assert driver_events(tape) == []
    assert at(tape, "asyncpg:connect") == []


def test_leaf_off_nests_the_driver_beneath_each_statement(
    postgresql: Server,
) -> None:
    with (
        instrumentation(AsyncpgInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation, leaf=False),
        timeline() as tape,
    ):
        workload(postgresql)

    events = driver_events(tape)
    assert [event.data["statement"] for event in events] == DRIVER_STATEMENTS

    # SQLAlchemy's asyncpg dialect prepares each statement and fetches
    # through the prepared statement, so the driver events beneath
    # do_execute are the prepared statement's own.

    for event in events:
        assert event.path.startswith("asyncpg.prepared_stmt:PreparedStatement.")
        parent = tape.parent_of(event)
        assert parent is not None and parent.path == EXECUTE

    # SQLAlchemy's dialect opens its connections through the package's
    # spelling of connect.

    (connect,) = at(tape, CONNECT)
    assert [child.path for child in tape.children_of(connect)] == ["asyncpg:connect"]

    (commit,) = at(tape, COMMIT)
    (child,) = tape.children_of(commit)
    assert child.path == "asyncpg.connection:Connection.execute"
    assert child.data["operation"] == "COMMIT"


def test_raw_driver_use_beside_the_engine_records_at_top_level(
    postgresql: Server,
) -> None:
    async def raw() -> None:
        connection = await asyncpg.connect(postgresql.url)
        try:
            await connection.fetchval("SELECT 2")
        finally:
            await connection.close()

    with (
        instrumentation(AsyncpgInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        workload(postgresql)
        asyncio.run(raw())

    (event,) = [
        event for event in tape.all if event.data.get("statement") == "SELECT 2"
    ]
    assert event.path == "asyncpg.connection:Connection.fetchval"
    assert tape.parent_of(event) is None
