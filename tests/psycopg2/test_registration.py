"""The entry point: resolving the instrumentation by its bare name,
and what the listing tool says about it."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg2")

import psycopg2
from wrapture import Config, InstrumentEntry, instrumentation, timeline

from tests.conftest import DISTRIBUTION, Server, run_tool
from wrapture_instrumentation_postgresql import __version__
from wrapture_instrumentation_postgresql.psycopg2 import Psycopg2Instrumentation


def test_the_bare_name_resolves_to_the_class() -> None:
    with instrumentation("psycopg2") as record:
        (instance,) = record.instrumentations

        assert type(instance) is Psycopg2Instrumentation
        assert instance.name == "psycopg2"
        assert instance.distribution == DISTRIBUTION
        assert instance.description == "Query and transaction tracing for psycopg2."


def test_a_config_entry_applies_and_reverts(postgresql: Server) -> None:
    applied = Config(instrument=[InstrumentEntry("psycopg2")]).apply()
    try:
        report = applied.report()
        assert "psycopg2" in report
        assert "applied psycopg2" in report

        with timeline() as tape:
            connection = psycopg2.connect(postgresql.url)
            with connection:
                connection.cursor().execute("SELECT 1")
            connection.close()

        assert [event.label or event.path for event in tape.all] == [
            "psycopg2:connect",
            "psycopg2.extensions:cursor.execute",
            "psycopg2.extensions:connection.__exit__",
        ]
    finally:
        applied.revert()

    with timeline() as tape:
        connection = psycopg2.connect(postgresql.url)
        with connection:
            connection.cursor().execute("SELECT 1")
        connection.close()

    assert tape.all == []


def test_the_listing_tool_describes_the_entry() -> None:
    output = run_tool("instrumentation", "--verbose")

    assert f"psycopg2  ({DISTRIBUTION} {__version__})" in output
    assert "  Query and transaction tracing for psycopg2." in output
    assert "supported (>=2.9,<3)" in output
    assert "  modules: psycopg2" in output

    # The listing pads the setting names into a column, so the name
    # and its description are checked apart.

    assert "    statement = false " in output
    assert "record the SQL text as handed to the driver on each query" in output


def test_the_toml_template_carries_the_settings() -> None:
    output = run_tool("instrumentation", "--toml")

    assert '[[instrument]]\nname = "psycopg2"\nenabled = false' in output
    assert "# statement = false" in output
