"""What the instrumentation records: the connect, the execute family,
streamed queries and COPY, the transaction boundaries and the
context managers, the server-side cursors, a pooled connection, and
what stays out of capture."""

from __future__ import annotations

import warnings
from typing import Any

import pytest

pytest.importorskip("psycopg")

import psycopg
from psycopg import sql
from wrapture import Event, RecordingGapWarning, Tape, instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_postgresql.psycopg import PsycopgInstrumentation

EXECUTE = "psycopg:Cursor.execute"
EXECUTEMANY = "psycopg:Cursor.executemany"


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


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_records_a_database_leaf(postgresql: Server, tape: Tape) -> None:
    psycopg.connect(postgresql.url).close()

    (event,) = at(tape, "psycopg:connect")
    contract(event, postgresql, "CONNECT")
    assert event.label is None
    assert event.arguments is None
    assert event.result == "<Connection>"
    assert tape.children_of(event) == []


def test_connect_never_captures_its_arguments(postgresql: Server, tape: Tape) -> None:
    # The conninfo and the keyword form both carry the password.

    psycopg.connect(postgresql.url).close()
    psycopg.connect(**postgresql.kwargs).close()
    psycopg.Connection.connect(postgresql.url).close()

    events = at(tape, "psycopg:connect") + at(tape, "psycopg:Connection.connect")
    assert len(events) == 3
    assert all(event.arguments is None for event in events)

    # Neither spelling of the credentials appears anywhere on the tape
    # (the user, database and password of a test server may coincide,
    # so the URL's password segment and the keyword are what to look
    # for).

    assert f":{postgresql.password}@" not in recorded(tape)
    assert "password" not in recorded(tape)


def test_a_connection_from_a_pool_records(postgresql: Server, tape: Tape) -> None:
    pool = pytest.importorskip("psycopg_pool")

    # The pool opens its connections on a worker thread of its own,
    # which carries no recording context, so the connect itself goes
    # unrecorded (wrapture warns of the gap); the queries on the
    # checked-out connection run on the caller's thread and record.

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RecordingGapWarning)

        with pool.ConnectionPool(postgresql.url, min_size=1, open=True) as pooled:
            with pooled.connection() as connection:
                connection.execute("SELECT 1").fetchone()

            # Returning the connection to the pool commits without
            # closing it.

            assert not connection.closed

    assert at(tape, "psycopg:Connection.connect") == []
    (execute,) = at(tape, EXECUTE)
    contract(execute, postgresql, "SELECT")
    (exit_event,) = at(tape, "psycopg:Connection.__exit__")
    assert exit_event.data["operation"] == "COMMIT"


# ---------------------------------------------------------------------------
# the execute family
# ---------------------------------------------------------------------------


def test_execute_records_operation_but_no_statement(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE secrets (value TEXT)")
            cursor.execute("INSERT INTO secrets VALUES (%s)", ("hunter2",))

    create, insert = at(tape, EXECUTE)
    contract(create, postgresql, "CREATE")
    contract(insert, postgresql, "INSERT")

    for event in (create, insert):
        assert "statement" not in event.data

    # The SQL reduces to its length and the parameters to a count in
    # the captured arguments, so the value never reaches the record.

    assert insert.arguments is not None
    assert insert.arguments["query"] == "<31 chars>"
    assert insert.arguments["params"] == "<1 values>"
    assert "hunter2" not in recorded(tape)


def test_the_statement_setting_records_the_text_as_handed_over(
    postgresql: Server,
) -> None:
    with (
        instrumentation(PsycopgInstrumentation, statement=True),
        timeline() as tape,
        psycopg.connect(postgresql.url) as connection,
    ):
        connection.execute("CREATE TEMP TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES (%(name)s)", {"name": "widget"})
        connection.execute(
            sql.SQL("SELECT name FROM {}").format(sql.Identifier("items"))
        ).fetchall()

    create, insert, select = at(tape, EXECUTE)
    assert create.data["statement"] == "CREATE TEMP TABLE items (name TEXT)"
    assert insert.data["statement"] == "INSERT INTO items VALUES (%(name)s)"
    assert insert.arguments is not None
    assert insert.arguments["params"] == "<1 values>"
    assert "widget" not in recorded(tape)

    # A composed query is rendered as the driver will send it.

    assert select.data["statement"] == 'SELECT name FROM "items"'
    assert select.data["operation"] == "SELECT"


def test_the_connection_shortcut_records_once_through_the_cursor(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)

    # Connection.execute builds a cursor and calls its execute, so the
    # one binding records it and nothing doubles it.

    (event,) = [event for event in tape.all if event.data.get("operation") == "SELECT"]
    assert event.path == EXECUTE


def test_executemany_records_a_count_and_never_iterates_a_generator(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url) as connection:
        connection.execute("CREATE TEMP TABLE items (name TEXT)")
        with connection.cursor() as cursor:
            cursor.executemany("INSERT INTO items VALUES (%s)", [("a",), ("b",)])
            cursor.executemany(
                "INSERT INTO items VALUES (%s)", (("g",) for _ in range(3))
            )
            cursor.execute("SELECT count(*) FROM items")
            assert cursor.fetchone() == (5,)

    listed, generated = at(tape, EXECUTEMANY)
    contract(listed, postgresql, "INSERT")
    assert listed.arguments is not None
    assert listed.arguments["params_seq"] == "<2 values>"
    assert generated.arguments is not None
    assert generated.arguments["params_seq"] == "<generator>"


def test_a_failing_query_records_its_exception(postgresql: Server, tape: Tape) -> None:
    with psycopg.connect(postgresql.url) as connection:
        with pytest.raises(psycopg.errors.UndefinedTable):
            connection.execute("SELECT nope FROM nowhere")

    (event,) = at(tape, EXECUTE)
    contract(event, postgresql, "SELECT")
    assert isinstance(event.exception, psycopg.errors.UndefinedTable)


def test_a_client_side_cursor_records_through_the_same_binding(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url, cursor_factory=psycopg.ClientCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT %s::text", ("s3cret-token",))
            assert cursor.fetchone() == ("s3cret-token",)

    # A client-side cursor interpolates the parameters into the query
    # before sending; the record still never sees them.

    (event,) = at(tape, EXECUTE)
    contract(event, postgresql, "SELECT")
    assert "s3cret-token" not in recorded(tape)


def test_pipeline_mode_records_every_statement(postgresql: Server, tape: Tape) -> None:
    with psycopg.connect(postgresql.url) as connection:
        with connection.pipeline():
            connection.execute("CREATE TEMP TABLE items (name TEXT)")
            connection.execute("INSERT INTO items VALUES (%s)", ("a",))
            connection.execute("INSERT INTO items VALUES (%s)", ("b",))

    assert [event.data["operation"] for event in at(tape, EXECUTE)] == [
        "CREATE",
        "INSERT",
        "INSERT",
    ]


# ---------------------------------------------------------------------------
# streamed queries, COPY and server-side cursors
# ---------------------------------------------------------------------------


def test_stream_records_around_the_iteration(postgresql: Server, tape: Tape) -> None:
    with psycopg.connect(postgresql.url) as connection:
        connection.execute("CREATE TEMP TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('a'), ('b'), ('c')")

        with connection.cursor() as cursor:
            rows = list(cursor.stream("SELECT name FROM items ORDER BY name"))

    assert rows == [("a",), ("b",), ("c",)]

    (event,) = at(tape, "psycopg:Cursor.stream")
    contract(event, postgresql, "SELECT")
    assert event.items == 3


def test_copy_in_records_a_block_spanning_the_transfer(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url) as connection:
        connection.execute("CREATE TEMP TABLE items (name TEXT)")

        with connection.cursor() as cursor:
            with cursor.copy("COPY items (name) FROM STDIN") as copy:
                copy.write_row(("a",))
                copy.write_row(("b",))

            cursor.execute("SELECT count(*) FROM items")
            assert cursor.fetchone() == (2,)

    (event,) = [event for event in tape.all if event.kind == "block"]
    assert event.label == "psycopg:Cursor.copy"
    contract(event, postgresql, "COPY")
    assert event.data["rows"] == 2
    assert "statement" not in event.data

    # The factory call itself records nothing: the block is the event.

    assert at(tape, "psycopg:Cursor.copy") == []


def test_copy_out_records_the_block_with_the_statement_when_on(
    postgresql: Server,
) -> None:
    with (
        instrumentation(PsycopgInstrumentation, statement=True),
        timeline() as tape,
        psycopg.connect(postgresql.url) as connection,
    ):
        connection.execute("CREATE TEMP TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('a'), ('b')")

        with connection.cursor() as cursor:
            with cursor.copy("COPY items TO STDOUT") as copy:
                received = b"".join(copy)

    assert received == b"a\nb\n"

    (event,) = [event for event in tape.all if event.kind == "block"]
    assert event.data["operation"] == "COPY"
    assert event.data["statement"] == "COPY items TO STDOUT"
    assert event.data["rows"] == 2


def test_a_server_side_cursor_records_its_declare_only(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url) as connection:
        connection.execute("CREATE TEMP TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('a'), ('b'), ('c')")

        with connection.cursor(name="items") as cursor:
            cursor.execute("SELECT name FROM items ORDER BY name")
            assert cursor.fetchmany(2) == [("a",), ("b",)]
            assert cursor.fetchall() == [("c",)]

    (event,) = at(tape, "psycopg:ServerCursor.execute")
    contract(event, postgresql, "DECLARE")

    # The FETCHes from the portal are not recorded: fetching is the
    # application's time, the model every database target follows.

    operations = [event.data.get("operation") for event in tape.all]
    assert "FETCH" not in operations


# ---------------------------------------------------------------------------
# transaction boundaries
# ---------------------------------------------------------------------------


def test_commit_and_rollback_record_their_operations(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url) as connection:
        connection.execute("CREATE TEMP TABLE items (name TEXT)")
        connection.execute("INSERT INTO items VALUES ('kept')")
        connection.commit()
        connection.execute("INSERT INTO items VALUES ('dropped')")
        connection.rollback()

        assert connection.execute("SELECT name FROM items").fetchall() == [("kept",)]

    (commit,) = at(tape, "psycopg:Connection.commit")
    contract(commit, postgresql, "COMMIT")
    (rollback,) = at(tape, "psycopg:Connection.rollback")
    contract(rollback, postgresql, "ROLLBACK")


def test_the_connection_context_manager_records_its_commit(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url) as connection:
        connection.execute("SELECT 1")

    (event,) = at(tape, "psycopg:Connection.__exit__")
    contract(event, postgresql, "COMMIT")
    assert connection.closed


def test_the_connection_context_manager_records_its_rollback(
    postgresql: Server, tape: Tape
) -> None:
    with pytest.raises(RuntimeError):
        with psycopg.connect(postgresql.url) as connection:
            connection.execute("SELECT 1")
            raise RuntimeError("abandon the transaction")

    (event,) = at(tape, "psycopg:Connection.__exit__")
    contract(event, postgresql, "ROLLBACK")

    # The exception's message is application data: the exit's captured
    # arguments carry types, never the value.

    assert event.arguments is not None
    assert event.arguments["exc_val"] == "<RuntimeError>"
    assert "abandon the transaction" not in recorded(tape)


def test_a_transaction_block_records_begin_and_commit(
    postgresql: Server, tape: Tape
) -> None:
    # On an autocommit connection nothing is open beforehand, so the
    # block itself begins the transaction.

    with psycopg.connect(postgresql.url, autocommit=True) as connection:
        with connection.transaction():
            connection.execute("SELECT 1")

    (enter,) = at(tape, "psycopg:Transaction.__enter__")
    contract(enter, postgresql, "BEGIN")
    assert "savepoint" not in enter.data
    (exit_event,) = at(tape, "psycopg:Transaction.__exit__")
    contract(exit_event, postgresql, "COMMIT")


def test_nested_transaction_blocks_record_savepoints(
    postgresql: Server, tape: Tape
) -> None:
    with psycopg.connect(postgresql.url, autocommit=True) as connection:
        connection.execute("CREATE TEMP TABLE items (name TEXT)")

        with connection.transaction():
            connection.execute("INSERT INTO items VALUES ('outer')")

            with connection.transaction(savepoint_name="inner"):
                connection.execute("INSERT INTO items VALUES ('released')")

            with pytest.raises(RuntimeError):
                with connection.transaction():
                    connection.execute("INSERT INTO items VALUES ('rolled back')")
                    raise RuntimeError("undo")

        rows = connection.execute("SELECT name FROM items ORDER BY name").fetchall()

    assert rows == [("outer",), ("released",)]

    enters = at(tape, "psycopg:Transaction.__enter__")
    assert [event.data["operation"] for event in enters] == [
        "BEGIN",
        "SAVEPOINT",
        "SAVEPOINT",
    ]
    assert enters[1].data["savepoint"] == "inner"

    exits = at(tape, "psycopg:Transaction.__exit__")
    assert [event.data["operation"] for event in exits] == [
        "RELEASE",
        "ROLLBACK",
        "COMMIT",
    ]
    assert exits[0].data["savepoint"] == "inner"
    assert "savepoint" in exits[1].data
    assert "savepoint" not in exits[2].data


def test_rollback_requests_record_a_rollback(postgresql: Server, tape: Tape) -> None:
    with psycopg.connect(postgresql.url, autocommit=True) as connection:
        with connection.transaction():
            raise psycopg.Rollback()

        with connection.transaction(force_rollback=True):
            connection.execute("SELECT 1")

    exits = at(tape, "psycopg:Transaction.__exit__")
    assert [event.data["operation"] for event in exits] == ["ROLLBACK", "ROLLBACK"]
    assert all(event.exception is None for event in exits)


# ---------------------------------------------------------------------------
# the shape of every event
# ---------------------------------------------------------------------------


def test_every_event_carries_the_contract_keys(postgresql: Server, tape: Tape) -> None:
    with psycopg.connect(postgresql.url) as connection:
        connection.execute("CREATE TEMP TABLE items (name TEXT)")
        with connection.transaction():
            connection.execute("INSERT INTO items VALUES ('a')")
        with connection.cursor() as cursor:
            with cursor.copy("COPY items (name) FROM STDIN") as copy:
                copy.write_row(("b",))
            list(cursor.stream("SELECT name FROM items"))
        connection.commit()

    keys: set[str] = {"system", "operation", "database", "host", "port"}
    for event in tape.all:
        assert keys <= set(event.data), event.path
        assert event.category == "database", event.path

    data: dict[str, Any] = tape.all[0].data
    assert data["system"] == "postgresql"
