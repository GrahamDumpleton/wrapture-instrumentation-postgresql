"""With the core package's sqlalchemy instrumentation applied
alongside, over the psycopg dialect: the default leaf keeps the
driver's events out of the tree, leaf off nests them beneath each
statement, and raw driver use beside the engine still records."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("sqlalchemy")
pytest.importorskip("wrapture_instrumentation")

import psycopg
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from wrapture import Event, Tape, instrumentation, timeline
from wrapture_instrumentation.database.sqlalchemy import SQLAlchemyInstrumentation

from tests.conftest import Server
from wrapture_instrumentation_postgresql.psycopg import PsycopgInstrumentation

EXECUTE = "sqlalchemy.engine.default:DefaultDialect.do_execute"
EXECUTEMANY = "sqlalchemy.engine.default:DefaultDialect.do_executemany"
CONNECT = "sqlalchemy.engine.default:DefaultDialect.connect"
COMMIT = "sqlalchemy.engine.base:Connection._commit_impl"

# The statements as SQLAlchemy is handed them, and as the driver
# then sees them (a named parameter compiles to psycopg's spelling).

WORKLOAD = [
    ("CREATE TEMP TABLE items (name TEXT)", "CREATE TEMP TABLE items (name TEXT)"),
    ("INSERT INTO items VALUES (:name)", "INSERT INTO items VALUES (%(name)s)"),
    ("SELECT name FROM items", "SELECT name FROM items"),
]
DRIVER_STATEMENTS = [driver for _, driver in WORKLOAD]


def at(tape: Tape, path: str) -> list[Event]:
    return [event for event in tape.all if event.path == path]


def driver_events(tape: Tape) -> list[Event]:
    """The psycopg events that belong to the workload, leaving out the
    dialect's own setup queries and the pool's reset-on-return
    rollback, which run outside SQLAlchemy's seams."""

    return [
        event
        for event in tape.all
        if event.path.startswith("psycopg:")
        and event.data.get("statement") in DRIVER_STATEMENTS
    ]


def engine_url(postgresql: Server) -> str:
    return postgresql.url.replace("postgresql://", "postgresql+psycopg://")


def workload(postgresql: Server) -> None:
    engine = create_engine(engine_url(postgresql))

    with engine.begin() as connection:
        connection.execute(text(WORKLOAD[0][0]))
        connection.execute(text(WORKLOAD[1][0]), [{"name": "a"}, {"name": "b"}])
        connection.execute(text(WORKLOAD[2][0])).fetchall()

    engine.dispose()


def test_the_default_leaf_keeps_the_driver_out(postgresql: Server) -> None:
    with (
        instrumentation(PsycopgInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        workload(postgresql)

    # The dialect's seams record the workload (and the dialect's own
    # setup queries beside it, which are do_execute calls too).

    assert len(at(tape, EXECUTE)) >= 2
    assert len(at(tape, EXECUTEMANY)) == 1
    assert len(at(tape, CONNECT)) == 1
    assert len(at(tape, COMMIT)) == 1

    # Every workload statement ran beneath a sqlalchemy leaf, so the
    # driver's own events for them stay out of the tape.

    assert driver_events(tape) == []
    assert at(tape, "psycopg:connect") == []
    assert at(tape, "psycopg:Connection.commit") == []


def test_leaf_off_nests_the_driver_beneath_each_statement(
    postgresql: Server,
) -> None:
    with (
        instrumentation(PsycopgInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation, leaf=False),
        timeline() as tape,
    ):
        workload(postgresql)

    events = driver_events(tape)
    assert [event.data["statement"] for event in events] == DRIVER_STATEMENTS

    # Each workload statement's driver event nests beneath the
    # dialect's seam that issued it.

    create, insert, select = events
    for event in (create, select):
        assert event.path == "psycopg:Cursor.execute"
        parent = tape.parent_of(event)
        assert parent is not None and parent.path == EXECUTE

    assert insert.path == "psycopg:Cursor.executemany"
    parent = tape.parent_of(insert)
    assert parent is not None and parent.path == EXECUTEMANY
    assert insert.arguments is not None
    assert insert.arguments["params_seq"] == "<2 values>"

    (connect,) = at(tape, CONNECT)
    assert [child.path for child in tape.children_of(connect)] == ["psycopg:connect"]

    (commit,) = at(tape, COMMIT)
    assert [child.path for child in tape.children_of(commit)] == [
        "psycopg:Connection.commit"
    ]


def test_the_async_engine_composes_the_same_way(postgresql: Server) -> None:
    async def run() -> None:
        engine = create_async_engine(engine_url(postgresql))

        async with engine.begin() as connection:
            await connection.execute(text(WORKLOAD[0][0]))
            (await connection.execute(text(WORKLOAD[2][0]))).fetchall()

        await engine.dispose()

    with (
        instrumentation(PsycopgInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation, leaf=False),
        timeline() as tape,
    ):
        asyncio.run(run())

    events = driver_events(tape)
    assert [event.data["statement"] for event in events] == [
        DRIVER_STATEMENTS[0],
        DRIVER_STATEMENTS[2],
    ]
    for event in events:
        assert event.path == "psycopg:AsyncCursor.execute"
        parent = tape.parent_of(event)
        assert parent is not None and parent.path == EXECUTE

    (connect,) = at(tape, CONNECT)
    assert [child.path for child in tape.children_of(connect)] == [
        "psycopg:AsyncConnection.connect"
    ]


def test_raw_driver_use_beside_the_engine_records_at_top_level(
    postgresql: Server,
) -> None:
    with (
        instrumentation(PsycopgInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        workload(postgresql)

        with psycopg.connect(postgresql.url) as connection:
            connection.execute("SELECT 2").fetchone()

    (raw,) = [event for event in tape.all if event.data.get("statement") == "SELECT 2"]
    assert raw.path == "psycopg:Cursor.execute"
    assert tape.parent_of(raw) is None
