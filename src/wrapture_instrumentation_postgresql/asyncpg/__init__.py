"""Instrumentation for asyncpg: every query, the connections opened
and the transaction boundaries recorded as database events, by
bindings on the pure Python classes the driver is made of, each
recording around its await.

This module imports only wrapture. Everything that touches asyncpg
lives in the sibling modules, one per asyncpg module patched
(connection.py for asyncpg and asyncpg.connection, prepared.py for
asyncpg.prepared_stmt, cursor.py for asyncpg.cursor), each importing
only wrapture at top level and reaching asyncpg through the module
the hook is handed, so loading this class when a config loads never
imports asyncpg ahead of the hooks meant to fire on its import.

Importing asyncpg initialises every one of those modules, so all four
hooks fire from the one import; each binds through the module it is
handed and cleans up its own bindings.
"""

from __future__ import annotations

from typing import Any

import wrapture
from wrapture import Setting

from . import connection, cursor, prepared


class AsyncpgInstrumentation(wrapture.Instrumentation):
    """Query and transaction tracing for asyncpg."""

    description = "Query and transaction tracing for asyncpg."

    target = "asyncpg"
    supports = ">=0.29,<1"
    removable = True

    settings = {
        "statement": Setting(
            False,
            "record the SQL text as handed to the driver on each query"
            " event; off by default because the driver cannot tell a"
            " literal an application interpolated from a placeholder,"
            " and the text is only safe to record when queries are"
            " parameterized",
        ),
    }

    @wrapture.instrumentation_hook("asyncpg")
    def asyncpg(self, name: str, module: Any) -> None:
        """Bind the package's own spelling of connect once asyncpg
        exists."""

        connection.instrument_package(module, self)

    @wrapture.instrumentation_hook("asyncpg.connection")
    def asyncpg_connection(self, name: str, module: Any) -> None:
        """Bind connect and the Connection methods once
        asyncpg.connection exists."""

        connection.instrument(module, self)

    @wrapture.instrumentation_hook("asyncpg.prepared_stmt")
    def asyncpg_prepared_stmt(self, name: str, module: Any) -> None:
        """Bind the prepared statement's own fetch family once
        asyncpg.prepared_stmt exists."""

        prepared.instrument(module, self)

    @wrapture.instrumentation_hook("asyncpg.cursor")
    def asyncpg_cursor(self, name: str, module: Any) -> None:
        """Bind the server-side cursor round trips once asyncpg.cursor
        exists."""

        cursor.instrument(module, self)
