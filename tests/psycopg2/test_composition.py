"""With the core package's sqlalchemy instrumentation applied
alongside, over the psycopg2 dialect: the default leaf keeps the
driver's events out of the tree, leaf off nests them beneath each
statement (the dialect's own do_executemany fast path included), and
raw driver use beside the engine still records."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")
pytest.importorskip("wrapture_instrumentation")

import psycopg2
from sqlalchemy import create_engine, text
from wrapture import Event, Tape, instrumentation, timeline
from wrapture_instrumentation.database.sqlalchemy import SQLAlchemyInstrumentation

from tests.conftest import Server
from wrapture_instrumentation_postgresql.psycopg2 import Psycopg2Instrumentation

EXECUTE = "sqlalchemy.engine.default:DefaultDialect.do_execute"
EXECUTEMANY = (
    "sqlalchemy.dialects.postgresql.psycopg2:PGDialect_psycopg2.do_executemany"
)
CONNECT = "sqlalchemy.engine.default:DefaultDialect.connect"
COMMIT = "sqlalchemy.engine.base:Connection._commit_impl"

# The statements as SQLAlchemy is handed them, and as the driver then
# sees them (a named parameter compiles to psycopg2's spelling).

WORKLOAD = [
    ("CREATE TEMP TABLE items (name TEXT)", "CREATE TEMP TABLE items (name TEXT)"),
    ("INSERT INTO items VALUES (:name)", "INSERT INTO items VALUES (%(name)s)"),
    ("SELECT name FROM items", "SELECT name FROM items"),
]
DRIVER_STATEMENTS = [driver for _, driver in WORKLOAD]


def at(tape: Tape, path: str) -> list[Event]:
    return [event for event in tape.all if event.path == path]


def driver_events(tape: Tape) -> list[Event]:
    """The psycopg2 events that belong to the workload, leaving out
    the dialect's own setup queries and the pool's reset-on-return
    rollback, which run outside SQLAlchemy's seams."""

    return [
        event
        for event in tape.all
        if (event.label or "").startswith("psycopg2.extensions:")
        and event.data.get("statement") in DRIVER_STATEMENTS
    ]


def engine_url(postgresql: Server) -> str:
    return postgresql.url.replace("postgresql://", "postgresql+psycopg2://")


def workload(postgresql: Server) -> None:
    engine = create_engine(engine_url(postgresql))

    with engine.begin() as connection:
        connection.execute(text(WORKLOAD[0][0]))
        connection.execute(text(WORKLOAD[1][0]), [{"name": "a"}, {"name": "b"}])
        connection.execute(text(WORKLOAD[2][0])).fetchall()

    engine.dispose()


def test_the_default_leaf_keeps_the_driver_out(postgresql: Server) -> None:
    with (
        instrumentation(Psycopg2Instrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        workload(postgresql)

    assert len(at(tape, EXECUTE)) >= 2
    assert len(at(tape, CONNECT)) == 1
    assert len(at(tape, COMMIT)) == 1

    # The psycopg2 dialect overrides do_executemany with its batch
    # helpers, bound by the sqlalchemy target in its own right.

    assert len(at(tape, EXECUTEMANY)) == 1

    assert driver_events(tape) == []
    assert at(tape, "psycopg2:connect") == []


def test_leaf_off_nests_the_driver_beneath_each_statement(
    postgresql: Server,
) -> None:
    with (
        instrumentation(Psycopg2Instrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation, leaf=False),
        timeline() as tape,
    ):
        workload(postgresql)

    events = driver_events(tape)
    assert [event.data["statement"] for event in events] == DRIVER_STATEMENTS

    # Each workload statement's driver event nests beneath the
    # dialect's seam that issued it: the psycopg2 dialect's own
    # do_executemany override for the executemany (a plain text()
    # statement takes the driver's executemany beneath it; the
    # dialect's execute_values batching applies to compiled inserts).

    create, insert, select = events
    for event in (create, select):
        assert event.label == "psycopg2.extensions:cursor.execute"
        parent = tape.parent_of(event)
        assert parent is not None and parent.path == EXECUTE

    assert insert.label == "psycopg2.extensions:cursor.executemany"
    parent = tape.parent_of(insert)
    assert parent is not None and parent.path == EXECUTEMANY
    assert insert.arguments is not None
    assert insert.arguments["vars_list"] == "<2 values>"

    (connect,) = at(tape, CONNECT)
    assert [child.path for child in tape.children_of(connect)] == ["psycopg2:connect"]

    (commit,) = at(tape, COMMIT)
    assert [child.label for child in tape.children_of(commit)] == [
        "psycopg2.extensions:connection.commit"
    ]


def test_raw_driver_use_beside_the_engine_records_at_top_level(
    postgresql: Server,
) -> None:
    with (
        instrumentation(Psycopg2Instrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        workload(postgresql)

        connection = psycopg2.connect(postgresql.url)
        try:
            connection.cursor().execute("SELECT 2")
        finally:
            connection.close()

    (raw,) = [event for event in tape.all if event.data.get("statement") == "SELECT 2"]
    assert raw.label == "psycopg2.extensions:cursor.execute"
    assert tape.parent_of(raw) is None
