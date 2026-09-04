"""Drive asyncpg against a PostgreSQL server with the instrumentation
applied.

The instrumentation is resolved by its entry point name and the
server is whatever WRAPTURE_POSTGRESQL_URL names (`just
postgresql-start` runs one and prints the URL). The calls cover the
shapes that matter: the connect, a table and inserts (one through
`executemany`), the fetch family, a transaction block with a nested
rollback (its boundaries recorded through execute), a prepared
statement, a server-side cursor, a COPY and a failing statement; then
the same with SQL text recording on. Each runs beneath an observed
coroutine, so the leaves sit in a tree.

Two views of the run always print: the live stream and the tree
reconstructed with timings. With --otel the same events also export
as OpenTelemetry spans to a local OTLP endpoint (http://localhost:4318
unless OTEL_EXPORTER_OTLP_ENDPOINT says otherwise), each a CLIENT
span carrying db.system.name and db.operation.name.
"""

from __future__ import annotations

import argparse
import asyncio
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
            " demo through `just demo-asyncpg --otel`, which overlays"
            " wrapture[otel] for the run"
        ) from error

    wrapture.add_sink(wrapture.otel.sink(service_name="wrapture-asyncpg-demo"))


@wrapture.observed
async def queries(url: str) -> None:
    """A table, some rows, the fetch family, a transaction block with
    a nested rollback, a prepared statement, a cursor, a COPY and a
    failing statement."""

    import asyncpg

    connection = await asyncpg.connect(url)
    try:
        await connection.execute("CREATE TEMP TABLE items (name TEXT)")
        await connection.execute("INSERT INTO items VALUES ($1)", "widget")
        await connection.executemany(
            "INSERT INTO items VALUES ($1)", [("gadget",), ("gizmo",)]
        )

        await connection.fetch("SELECT name FROM items ORDER BY name")
        await connection.fetchrow("SELECT count(*) FROM items")
        await connection.fetchval("SELECT max(name) FROM items")

        async with connection.transaction():
            await connection.execute("INSERT INTO items VALUES ('kept')")

            try:
                async with connection.transaction():
                    await connection.execute("INSERT INTO items VALUES ('undone')")
                    raise RuntimeError("undo the inner block")
            except RuntimeError:
                pass

        statement = await connection.prepare("SELECT name FROM items WHERE name = $1")
        await statement.fetchval("widget")

        async with connection.transaction():
            async for _ in connection.cursor("SELECT name FROM items", prefetch=2):
                pass

        await connection.copy_records_to_table("items", records=[("copied",)])
        await connection.copy_from_table("items", output=io.BytesIO())

        try:
            await connection.fetch("SELECT nope FROM nowhere")
        except asyncpg.UndefinedTableError:
            pass
    finally:
        await connection.close()


def main(arguments: list[str] | None = None) -> None:
    """Run the demo: apply the instrumentation, drive asyncpg against
    the server, print the live stream and the tree, and flush any
    exporters."""

    parser = argparse.ArgumentParser(
        prog="demo.asyncpg",
        description="Drive asyncpg against a PostgreSQL server with the"
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
        with wrapture.instrumentation("asyncpg"):
            asyncio.run(queries(url))

        with wrapture.instrumentation("asyncpg", statement=True):
            asyncio.run(queries(url))

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
        print(f"spans flushed to {endpoint} as service wrapture-asyncpg-demo")


if __name__ == "__main__":
    main()
