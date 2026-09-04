"""Applying and removing: the patched names on the package and its
classes, and that removal leaves them all as they were whatever the
setting."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

import psycopg
from wrapture import instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_postgresql.psycopg import PsycopgInstrumentation


def choke_points() -> dict[str, object]:
    """The callables currently at every patched name."""

    return {
        "connect": psycopg.connect,
        "class_connect": psycopg.Connection.__dict__["connect"],
        "async_class_connect": psycopg.AsyncConnection.__dict__["connect"],
        "cursor_execute": psycopg.Cursor.execute,
        "cursor_executemany": psycopg.Cursor.executemany,
        "cursor_stream": psycopg.Cursor.stream,
        "cursor_copy": psycopg.Cursor.copy,
        "async_cursor_execute": psycopg.AsyncCursor.execute,
        "async_cursor_stream": psycopg.AsyncCursor.stream,
        "async_cursor_copy": psycopg.AsyncCursor.copy,
        "server_cursor_execute": psycopg.ServerCursor.execute,
        "async_server_cursor_execute": psycopg.AsyncServerCursor.execute,
        "commit": psycopg.Connection.commit,
        "rollback": psycopg.Connection.rollback,
        "exit": psycopg.Connection.__exit__,
        "async_commit": psycopg.AsyncConnection.commit,
        "async_exit": psycopg.AsyncConnection.__aexit__,
        "transaction_enter": psycopg.Transaction.__enter__,
        "transaction_exit": psycopg.Transaction.__exit__,
        "async_transaction_enter": psycopg.AsyncTransaction.__aenter__,
        "async_transaction_exit": psycopg.AsyncTransaction.__aexit__,
    }


@pytest.mark.parametrize("statement", [False, True])
def test_apply_then_remove_leaves_everything_as_it_was(statement: bool) -> None:
    # The statement setting shapes the recorded data, not the patch,
    # so the patched set is the same either way.

    before = choke_points()

    with instrumentation(PsycopgInstrumentation, statement=statement) as record:
        (instance,) = record.instrumentations

        assert instance.applied == ("psycopg",)

        current = choke_points()
        for name in before:
            assert current[name] is not before[name], name

    current = choke_points()
    for name in before:
        assert current[name] is before[name], name

    assert not instance.applied


def test_after_removal_a_query_records_nothing(postgresql: Server) -> None:
    with instrumentation(PsycopgInstrumentation):
        pass

    with timeline() as tape, psycopg.connect(postgresql.url) as connection:
        connection.execute("SELECT 1").fetchone()

    assert tape.all == []


def test_a_connection_opened_while_applied_stops_recording_on_removal(
    postgresql: Server,
) -> None:
    # The bindings sit on the classes, so a connection that outlives
    # the instrumentation keeps working and simply stops recording.

    with instrumentation(PsycopgInstrumentation):
        connection = psycopg.connect(postgresql.url)

    try:
        with timeline() as tape:
            assert connection.execute("SELECT 1").fetchone() == (1,)

        assert tape.all == []
    finally:
        connection.close()
