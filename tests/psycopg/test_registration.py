"""The entry point: resolving the instrumentation by its bare name,
and what the listing tool says about it."""

from __future__ import annotations

from importlib import metadata

import pytest

pytest.importorskip("psycopg")

import psycopg
from wrapture import Config, InstrumentEntry, instrumentation, timeline

from tests.conftest import DISTRIBUTION, Server, run_tool
from wrapture_instrumentation_postgresql import __version__
from wrapture_instrumentation_postgresql.psycopg import PsycopgInstrumentation


def test_the_bare_name_resolves_to_the_class() -> None:
    with instrumentation("psycopg") as record:
        (instance,) = record.instrumentations

        assert type(instance) is PsycopgInstrumentation
        assert instance.name == "psycopg"
        assert instance.distribution == DISTRIBUTION
        assert instance.description == (
            "Query and transaction tracing for psycopg (version 3)."
        )


def test_a_config_entry_applies_and_reverts(postgresql: Server) -> None:
    applied = Config(instrument=[InstrumentEntry("psycopg")]).apply()
    try:
        report = applied.report()
        assert "psycopg" in report
        assert f"target psycopg {metadata.version('psycopg')}" in report
        assert "applied psycopg" in report

        with timeline() as tape, psycopg.connect(postgresql.url) as connection:
            connection.execute("SELECT 1").fetchone()

        assert [event.path for event in tape.all] == [
            "psycopg:connect",
            "psycopg:Cursor.execute",
            "psycopg:Connection.__exit__",
        ]
    finally:
        applied.revert()

    with timeline() as tape, psycopg.connect(postgresql.url) as connection:
        connection.execute("SELECT 1").fetchone()

    assert tape.all == []


def test_the_listing_tool_describes_the_entry() -> None:
    output = run_tool("instrumentation", "--verbose")

    assert f"psycopg  ({DISTRIBUTION} {__version__})" in output
    assert "  Query and transaction tracing for psycopg (version 3)." in output
    assert (
        f"  target: psycopg {metadata.version('psycopg')}, supported (>=3.1,<4)"
        in output
    )
    assert "  modules: psycopg" in output

    # The listing pads the setting names into a column, so the name
    # and its description are checked apart.

    assert "    statement = false " in output
    assert "record the SQL text as handed to the driver on each query" in output


def test_the_toml_template_carries_the_settings() -> None:
    output = run_tool("instrumentation", "--toml")

    assert '[[instrument]]\nname = "psycopg"\nenabled = false' in output
    assert "# statement = false" in output
