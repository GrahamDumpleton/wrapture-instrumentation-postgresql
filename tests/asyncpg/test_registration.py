"""The entry point: resolving the instrumentation by its bare name,
and what the listing tool says about it."""

from __future__ import annotations

import asyncio
from importlib import metadata

import pytest

pytest.importorskip("asyncpg")

import asyncpg
from wrapture import Config, InstrumentEntry, instrumentation, timeline

from tests.conftest import DISTRIBUTION, Server, run_tool
from wrapture_instrumentation_postgresql import __version__
from wrapture_instrumentation_postgresql.asyncpg import AsyncpgInstrumentation


def test_the_bare_name_resolves_to_the_class() -> None:
    with instrumentation("asyncpg") as record:
        (instance,) = record.instrumentations

        assert type(instance) is AsyncpgInstrumentation
        assert instance.name == "asyncpg"
        assert instance.distribution == DISTRIBUTION
        assert instance.description == "Query and transaction tracing for asyncpg."


async def workload(url: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.fetchval("SELECT 1")
    finally:
        await connection.close()


def test_a_config_entry_applies_and_reverts(postgresql: Server) -> None:
    applied = Config(instrument=[InstrumentEntry("asyncpg")]).apply()
    try:
        report = applied.report()
        assert "asyncpg" in report
        assert f"target asyncpg {metadata.version('asyncpg')}" in report
        assert "applied asyncpg" in report

        with timeline() as tape:
            asyncio.run(workload(postgresql.url))

        assert [event.path for event in tape.all] == [
            "asyncpg:connect",
            "asyncpg.connection:Connection.fetchval",
        ]
    finally:
        applied.revert()

    with timeline() as tape:
        asyncio.run(workload(postgresql.url))

    assert tape.all == []


def test_the_listing_tool_describes_the_entry() -> None:
    output = run_tool("instrumentation", "--verbose")

    assert f"asyncpg  ({DISTRIBUTION} {__version__})" in output
    assert "  Query and transaction tracing for asyncpg." in output
    assert (
        f"  target: asyncpg {metadata.version('asyncpg')}, supported (>=0.29,<1)"
        in output
    )
    assert "  modules: asyncpg, asyncpg.connection" in output

    assert "    statement = false " in output
    assert "record the SQL text as handed to the driver on each query" in output


def test_the_toml_template_carries_the_settings() -> None:
    output = run_tool("instrumentation", "--toml")

    assert '[[instrument]]\nname = "asyncpg"\nenabled = false' in output
    assert "# statement = false" in output
