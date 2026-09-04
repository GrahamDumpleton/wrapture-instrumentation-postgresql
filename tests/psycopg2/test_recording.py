"""What the instrumentation records: the connect, the execute family,
COPY, the transaction boundaries and the context manager, the
application's own factories still working and recorded, psycopg2's
C entry points accepting the recording objects, and what stays out
of capture."""

from __future__ import annotations

import io
import uuid

import pytest

pytest.importorskip("psycopg2")

import psycopg2
import psycopg2.extensions
import psycopg2.extras
from psycopg2 import sql
from wrapture import Event, Tape, instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_postgresql.psycopg2 import (
    Psycopg2Instrumentation,
    factories,
)

EXECUTE = "psycopg2.extensions:cursor.execute"
EXECUTEMANY = "psycopg2.extensions:cursor.executemany"
COMMIT = "psycopg2.extensions:connection.commit"
ROLLBACK = "psycopg2.extensions:connection.rollback"
EXIT = "psycopg2.extensions:connection.__exit__"


def labelled(tape: Tape, label: str) -> list[Event]:
    return [event for event in tape.all if event.label == label]


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
    psycopg2.connect(postgresql.url).close()

    (event,) = at(tape, "psycopg2:connect")
    contract(event, postgresql, "CONNECT")
    assert event.label is None
    assert event.arguments is None
    assert event.result == "<connection>"
    assert tape.children_of(event) == []


def test_connect_never_captures_its_arguments(postgresql: Server, tape: Tape) -> None:
    psycopg2.connect(postgresql.url).close()
    psycopg2.connect(**postgresql.kwargs).close()

    events = at(tape, "psycopg2:connect")
    assert len(events) == 2
    assert all(event.arguments is None for event in events)
    assert f":{postgresql.password}@" not in recorded(tape)
    assert "password" not in recorded(tape)


def test_the_connection_is_a_real_subclass_that_reads_as_before(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        cursor = connection.cursor()
        recording_classes = (
            isinstance(connection, factories.ConnectionMixin),
            isinstance(cursor, factories.CursorMixin),
        )
        assert recording_classes == (True, True)

        assert isinstance(connection, psycopg2.extensions.connection)
        assert type(connection).__name__ == "connection"
        assert type(connection).__module__ == "psycopg2.extensions"
        assert isinstance(cursor, psycopg2.extensions.cursor)
        assert type(cursor).__name__ == "cursor"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# the execute family
# ---------------------------------------------------------------------------


def test_execute_records_operation_but_no_statement(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMP TABLE secrets (value TEXT)")
        cursor.execute("INSERT INTO secrets VALUES (%s)", ("hunter2",))
    finally:
        connection.close()

    create, insert = labelled(tape, EXECUTE)
    contract(create, postgresql, "CREATE")
    contract(insert, postgresql, "INSERT")

    for event in (create, insert):
        assert "statement" not in event.data
        assert event.path == (
            "wrapture_instrumentation_postgresql.psycopg2.factories:CursorMixin.execute"
        )

    # The SQL reduces to its length and the parameters to a count in
    # the captured arguments, so the value never reaches the record.

    assert insert.arguments is not None
    assert insert.arguments["query"] == "<31 chars>"
    assert insert.arguments["vars"] == "<1 values>"
    assert "hunter2" not in recorded(tape)


def test_the_statement_setting_records_the_text_with_its_placeholders(
    postgresql: Server,
) -> None:
    with (
        instrumentation(Psycopg2Instrumentation, statement=True),
        timeline() as tape,
    ):
        connection = psycopg2.connect(postgresql.url)
        try:
            cursor = connection.cursor()
            cursor.execute("CREATE TEMP TABLE items (name TEXT)")
            cursor.execute("INSERT INTO items VALUES (%(name)s)", {"name": "widget"})
            cursor.execute(
                sql.SQL("SELECT name FROM {}").format(sql.Identifier("items"))
            )
            assert cursor.fetchall() == [("widget",)]
        finally:
            connection.close()

    create, insert, select = labelled(tape, EXECUTE)
    assert create.data["statement"] == "CREATE TEMP TABLE items (name TEXT)"

    # psycopg2 interpolates the parameters client-side, below the
    # seam, so the recorded text is the template with its
    # placeholders, never the value.

    assert insert.data["statement"] == "INSERT INTO items VALUES (%(name)s)"
    assert "widget" not in recorded(tape)

    # A composed query is rendered as the driver will send it.

    assert select.data["statement"] == 'SELECT name FROM "items"'
    assert select.data["operation"] == "SELECT"


def test_executemany_records_a_count_and_never_iterates_a_generator(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMP TABLE items (name TEXT)")
        cursor.executemany("INSERT INTO items VALUES (%s)", [("a",), ("b",)])
        cursor.executemany("INSERT INTO items VALUES (%s)", (("g",) for _ in range(3)))
        cursor.execute("SELECT count(*) FROM items")
        assert cursor.fetchone() == (5,)
    finally:
        connection.close()

    listed, generated = labelled(tape, EXECUTEMANY)
    contract(listed, postgresql, "INSERT")
    assert listed.arguments is not None
    assert listed.arguments["vars_list"] == "<2 values>"
    assert generated.arguments is not None
    assert generated.arguments["vars_list"] == "<generator>"


def test_callproc_records_a_call(postgresql: Server, tape: Tape) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        cursor = connection.cursor()
        cursor.callproc("pg_backend_pid")
        (pid,) = cursor.fetchone()
        assert isinstance(pid, int)
    finally:
        connection.close()

    (event,) = labelled(tape, "psycopg2.extensions:cursor.callproc")
    contract(event, postgresql, "CALL")
    assert event.data["procedure"] == "pg_backend_pid"


def test_a_failing_query_records_its_exception(postgresql: Server, tape: Tape) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        with pytest.raises(psycopg2.errors.UndefinedTable):
            connection.cursor().execute("SELECT nope FROM nowhere")
    finally:
        connection.close()

    (event,) = labelled(tape, EXECUTE)
    contract(event, postgresql, "SELECT")
    assert isinstance(event.exception, psycopg2.errors.UndefinedTable)


def test_the_extras_batch_helpers_record_one_event_per_batch(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMP TABLE items (n INT)")
        psycopg2.extras.execute_values(
            cursor, "INSERT INTO items VALUES %s", [(1,), (2,), (3,)], page_size=2
        )
        psycopg2.extras.execute_batch(
            cursor, "INSERT INTO items VALUES (%s)", [(4,), (5,)]
        )
        cursor.execute("SELECT count(*) FROM items")
        assert cursor.fetchone() == (5,)
    finally:
        connection.close()

    # execute_values sends one statement per page (two pages here)
    # and execute_batch one statement joining the batch: each is an
    # execute on the wire, and records as one.

    inserts = [
        event
        for event in labelled(tape, EXECUTE)
        if event.data["operation"] == "INSERT"
    ]
    assert len(inserts) == 3


# ---------------------------------------------------------------------------
# COPY
# ---------------------------------------------------------------------------


def test_copy_from_and_copy_to_record_the_table(postgresql: Server, tape: Tape) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMP TABLE items (name TEXT)")
        cursor.copy_from(io.StringIO("a\nb\n"), "items", columns=("name",))

        received = io.StringIO()
        cursor.copy_to(received, "items", columns=("name",))
        assert received.getvalue() == "a\nb\n"
    finally:
        connection.close()

    (copy_in,) = labelled(tape, "psycopg2.extensions:cursor.copy_from")
    contract(copy_in, postgresql, "COPY")
    assert copy_in.data["collection"] == "items"
    assert copy_in.arguments is not None
    assert copy_in.arguments["file"] == "<StringIO>"
    assert copy_in.arguments["table"] == "items"

    (copy_out,) = labelled(tape, "psycopg2.extensions:cursor.copy_to")
    contract(copy_out, postgresql, "COPY")
    assert copy_out.data["collection"] == "items"


def test_copy_expert_records_the_statement_when_on(postgresql: Server) -> None:
    with (
        instrumentation(Psycopg2Instrumentation, statement=True),
        timeline() as tape,
    ):
        connection = psycopg2.connect(postgresql.url)
        try:
            cursor = connection.cursor()
            cursor.execute("CREATE TEMP TABLE items (name TEXT)")
            cursor.copy_expert("COPY items (name) FROM STDIN", io.StringIO("a\n"))
        finally:
            connection.close()

    (event,) = labelled(tape, "psycopg2.extensions:cursor.copy_expert")
    contract(event, postgresql, "COPY")
    assert event.data["statement"] == "COPY items (name) FROM STDIN"
    assert event.arguments is not None
    assert event.arguments["sql"] == "<28 chars>"
    assert event.arguments["file"] == "<StringIO>"


# ---------------------------------------------------------------------------
# transaction boundaries
# ---------------------------------------------------------------------------


def test_commit_and_rollback_record_their_operations(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMP TABLE items (name TEXT)")
        cursor.execute("INSERT INTO items VALUES ('kept')")
        connection.commit()
        cursor.execute("INSERT INTO items VALUES ('dropped')")
        connection.rollback()

        cursor.execute("SELECT name FROM items")
        assert cursor.fetchall() == [("kept",)]
    finally:
        connection.close()

    (commit,) = labelled(tape, COMMIT)
    contract(commit, postgresql, "COMMIT")
    (rollback,) = labelled(tape, ROLLBACK)
    contract(rollback, postgresql, "ROLLBACK")


def test_the_context_manager_records_its_commit_and_does_not_close(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")

        # psycopg2's exit commits or rolls back; it never closes.

        assert not connection.closed
    finally:
        connection.close()

    (event,) = labelled(tape, EXIT)
    contract(event, postgresql, "COMMIT")


def test_the_context_manager_records_its_rollback(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        with pytest.raises(RuntimeError):
            with connection:
                connection.cursor().execute("SELECT 1")
                raise RuntimeError("abandon the transaction")
    finally:
        connection.close()

    (event,) = labelled(tape, EXIT)
    contract(event, postgresql, "ROLLBACK")

    # The exception's message is application data: the exit's captured
    # arguments carry types, never the value.

    assert event.arguments is not None
    assert event.arguments["exc_value"] == "<RuntimeError>"
    assert "abandon the transaction" not in recorded(tape)


# ---------------------------------------------------------------------------
# the application's own factories, and psycopg2's C entry points
# ---------------------------------------------------------------------------


def test_a_cursor_factory_named_at_connect_is_recorded(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(
        postgresql.url, cursor_factory=psycopg2.extras.RealDictCursor
    )
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1 AS n")
        assert cursor.fetchone() == {"n": 1}
        assert isinstance(cursor, psycopg2.extras.RealDictCursor)
        assert type(cursor).__name__ == "RealDictCursor"
    finally:
        connection.close()

    (event,) = labelled(tape, EXECUTE)
    contract(event, postgresql, "SELECT")


def test_a_cursor_factory_named_per_cursor_is_recorded(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        by_keyword = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        by_keyword.execute("SELECT 1 AS n")
        assert by_keyword.fetchone()["n"] == 1

        # The positional spelling: cursor(name, cursor_factory).

        named = connection.cursor("named", psycopg2.extras.NamedTupleCursor)
        named.execute("SELECT 2 AS n")
        assert named.fetchone().n == 2
        named.close()
    finally:
        connection.close()

    assert [event.data["operation"] for event in labelled(tape, EXECUTE)] == [
        "SELECT",
        "SELECT",
    ]


def test_a_connection_factory_with_its_own_cursor_default_is_recorded(
    postgresql: Server, tape: Tape
) -> None:
    # The extras' connection classes override cursor() to supply their
    # own cursor class; that override runs, and the cursor it builds
    # is reclassed to record.

    connection = psycopg2.connect(
        postgresql.url, connection_factory=psycopg2.extras.RealDictConnection
    )
    try:
        assert isinstance(connection, psycopg2.extras.RealDictConnection)
        cursor = connection.cursor()
        assert isinstance(cursor, psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT 1 AS n")
        assert cursor.fetchone() == {"n": 1}
    finally:
        connection.close()

    (event,) = labelled(tape, EXECUTE)
    contract(event, postgresql, "SELECT")


def test_a_logging_connection_keeps_logging_and_records(
    postgresql: Server, tape: Tape
) -> None:
    log = io.StringIO()
    connection = psycopg2.connect(
        postgresql.url, connection_factory=psycopg2.extras.LoggingConnection
    )
    try:
        connection.initialize(log)
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)
    finally:
        connection.close()

    assert "SELECT 1" in log.getvalue()
    (event,) = labelled(tape, EXECUTE)
    contract(event, postgresql, "SELECT")


def test_the_c_entry_points_accept_the_recording_objects(
    postgresql: Server, tape: Tape
) -> None:
    # register_type and its callers type-check their argument in C;
    # a proxy would fail here, a real subclass passes.

    connection = psycopg2.connect(postgresql.url)
    try:
        psycopg2.extras.register_uuid(conn_or_curs=connection)
        cursor = connection.cursor()
        psycopg2.extras.register_default_json(cursor)

        value = uuid.uuid4()
        cursor.execute(
            "SELECT %s::uuid, %s::json", (value, psycopg2.extras.Json({"a": 1}))
        )
        assert cursor.fetchone() == (value, {"a": 1})

        assert psycopg2.extensions.quote_ident("x", connection) == '"x"'
        assert psycopg2.extensions.quote_ident("y", cursor) == '"y"'
    finally:
        connection.close()

    (event,) = labelled(tape, EXECUTE)
    contract(event, postgresql, "SELECT")
    assert str(value) not in recorded(tape)


def test_a_named_cursor_records_its_declare_only(
    postgresql: Server, tape: Tape
) -> None:
    connection = psycopg2.connect(postgresql.url)
    try:
        setup = connection.cursor()
        setup.execute("CREATE TEMP TABLE items (name TEXT)")
        setup.execute("INSERT INTO items VALUES ('a'), ('b'), ('c')")

        with connection.cursor(name="items") as cursor:
            cursor.execute("SELECT name FROM items ORDER BY name")
            assert cursor.fetchmany(2) == [("a",), ("b",)]
            assert cursor.fetchall() == [("c",)]
    finally:
        connection.close()

    # The named cursor's execute is the DECLARE; the FETCHes from the
    # portal are the application's time and are not recorded.

    events = labelled(tape, EXECUTE)
    assert [event.data["operation"] for event in events] == [
        "CREATE",
        "INSERT",
        "SELECT",
    ]
    assert "FETCH" not in [event.data.get("operation") for event in tape.all]
