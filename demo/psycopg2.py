"""Drive psycopg2 against a PostgreSQL server with the instrumentation
applied.

The instrumentation is resolved by its entry point name and the
server is whatever WRAPTURE_POSTGRESQL_URL names (`just
postgresql-start` runs one and prints the URL). The calls cover the
shapes that matter: the connect, a table and inserts (one through
`executemany`, one through the extras' `execute_values`), a COPY in
through a dict-row cursor, a select, a commit, a rollback, the
connection's context manager, and a failing statement; then the same
with SQL text recording on. Each runs beneath an observed function,
so the leaves sit in a tree.

Two views of the run always print: the live stream and the tree
reconstructed with timings. With --otel the same events also export
as OpenTelemetry spans to a local OTLP endpoint (http://localhost:4318
unless OTEL_EXPORTER_OTLP_ENDPOINT says otherwise), each a CLIENT
span carrying db.system.name and db.operation.name.
"""

from __future__ import annotations

import argparse
import io
import os
import sys

import wrapture


def add_otel_sink() -> None:
    """Register the OpenTelemetry sink; exits with guidance when the
    optional dependencies are missing."""

    try:
        import wrapture.otel
    except ImportError as error:
        raise SystemExit(
            "the OpenTelemetry dependencies are not installed; run the"
            " demo through `just demo-psycopg2 --otel`, which overlays"
            " wrapture[otel] for the run"
        ) from error

    wrapture.add_sink(wrapture.otel.sink(service_name="wrapture-psycopg2-demo"))


@wrapture.observed
def queries(url: str) -> None:
    """A table, some rows, a COPY, a select, a commit and a rollback,
    the context manager, and a failing statement."""

    import psycopg2
    import psycopg2.extras

    connection = psycopg2.connect(url, cursor_factory=psycopg2.extras.DictCursor)
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMP TABLE items (name TEXT)")
        cursor.execute("INSERT INTO items VALUES (%s)", ("widget",))
        cursor.executemany("INSERT INTO items VALUES (%s)", [("gadget",), ("gizmo",)])
        psycopg2.extras.execute_values(
            cursor, "INSERT INTO items VALUES %s", [("doodad",), ("gubbins",)]
        )
        cursor.copy_from(io.StringIO("copied\n"), "items", columns=("name",))
        connection.commit()

        cursor.execute("SELECT name FROM items ORDER BY name")
        cursor.fetchall()

        cursor.execute("INSERT INTO items VALUES ('undone')")
        connection.rollback()

        with connection:
            cursor.execute("INSERT INTO items VALUES ('kept')")

        try:
            cursor.execute("SELECT nope FROM nowhere")
        except psycopg2.errors.UndefinedTable:
            connection.rollback()
    finally:
        connection.close()


def main(arguments: list[str] | None = None) -> None:
    """Run the demo: apply the instrumentation, drive psycopg2 against
    the server, print the live stream and the tree, and flush any
    exporters."""

    parser = argparse.ArgumentParser(
        prog="demo.psycopg2",
        description="Drive psycopg2 against a PostgreSQL server with the"
        " instrumentation applied, printing the live stream and the tree.",
    )
    parser.add_argument(
        "--otel",
        action="store_true",
        help="also export the events as OpenTelemetry spans over OTLP",
    )
    options = parser.parse_args(arguments)

    url = os.environ.get("WRAPTURE_POSTGRESQL_URL")
    if not url:
        raise SystemExit(
            "WRAPTURE_POSTGRESQL_URL is not set; `just postgresql-start` runs"
            " a server and prints the line to export"
        )

    if options.otel:
        add_otel_sink()

    wrapture.add_sink(wrapture.Printer(stream=sys.stdout))

    print("== live stream ==")

    with wrapture.timeline() as tape:
        with wrapture.instrumentation("psycopg2"):
            queries(url)

        with wrapture.instrumentation("psycopg2", statement=True):
            queries(url)

    print()
    print("== tree ==")
    print(tape.tree(times=True))

    wrapture.shutdown()

    if options.otel:
        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
        )
        print()
        print("== otel ==")
        print(f"spans flushed to {endpoint} as service wrapture-psycopg2-demo")


if __name__ == "__main__":
    main()
