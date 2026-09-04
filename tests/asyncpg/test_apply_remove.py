"""Applying and removing: the patched names on the modules and
classes, and that removal leaves them all as they were whatever the
setting."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("asyncpg")

import asyncpg
import asyncpg.connection
import asyncpg.cursor
import asyncpg.prepared_stmt
from wrapture import instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_postgresql.asyncpg import AsyncpgInstrumentation


def choke_points() -> dict[str, object]:
    """The callables currently at every patched name."""

    points: dict[str, object] = {
        "connect": asyncpg.connect,
        "connection_connect": asyncpg.connection.connect,
        "explain": asyncpg.prepared_stmt.PreparedStatement.explain,
        "bind_exec": asyncpg.cursor.BaseCursor._bind_exec,
        "bind": asyncpg.cursor.BaseCursor._bind,
        "exec": asyncpg.cursor.BaseCursor._exec,
        "forward": asyncpg.cursor.Cursor.forward,
    }
    for method in (
        "execute",
        "executemany",
        "fetch",
        "fetchrow",
        "fetchval",
        "copy_from_table",
        "copy_to_table",
        "copy_records_to_table",
        "copy_from_query",
    ):
        points[method] = getattr(asyncpg.connection.Connection, method)
    for method in ("fetch", "fetchrow", "fetchval", "executemany"):
        points[f"prepared_{method}"] = getattr(
            asyncpg.prepared_stmt.PreparedStatement, method
        )

    return points


@pytest.mark.parametrize("statement", [False, True])
def test_apply_then_remove_leaves_everything_as_it_was(statement: bool) -> None:
    before = choke_points()

    with instrumentation(AsyncpgInstrumentation, statement=statement) as record:
        (instance,) = record.instrumentations

        assert len(instance.applied) == 4

        current = choke_points()
        for name in before:
            assert current[name] is not before[name], name

    current = choke_points()
    for name in before:
        assert current[name] is before[name], name

    assert not instance.applied


def test_after_removal_a_query_records_nothing(postgresql: Server) -> None:
    with instrumentation(AsyncpgInstrumentation):
        pass

    async def workload() -> None:
        connection = await asyncpg.connect(postgresql.url)
        try:
            await connection.fetchval("SELECT 1")
        finally:
            await connection.close()

    with timeline() as tape:
        asyncio.run(workload())

    assert tape.all == []
