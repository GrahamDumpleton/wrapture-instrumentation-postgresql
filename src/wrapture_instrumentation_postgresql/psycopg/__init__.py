"""Instrumentation for psycopg (version 3): every query, the
connections opened and the transaction boundaries recorded as
database events, by bindings on the pure Python classes the driver
is made of.

This module imports only wrapture. Everything that touches psycopg
lives in the sibling modules, one per kind of seam (cursor.py for the
execute family, stream and COPY; connection.py for connect, commit,
rollback and the connection's context manager; transaction.py for
`transaction()` blocks), each importing only wrapture at top level
and reaching psycopg through the package the hook is handed, so
loading this class when a config loads never imports psycopg ahead of
the hook meant to fire on its import.

One trigger suffices: importing psycopg initialises every submodule
the seams live in, so by the time the hook fires all the classes
exist under the package.
"""

from __future__ import annotations

from typing import Any

import wrapture
from wrapture import Setting

from . import connection, cursor, transaction


class PsycopgInstrumentation(wrapture.Instrumentation):
    """Query and transaction tracing for psycopg (version 3)."""

    description = "Query and transaction tracing for psycopg (version 3)."

    target = "psycopg"
    supports = ">=3.1,<4"
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

    @wrapture.instrumentation_hook("psycopg")
    def psycopg(self, name: str, module: Any) -> None:
        """Bind the cursor, connection and transaction seams once
        psycopg exists."""

        cursor.instrument(module, self)
        connection.instrument(module, self)
        transaction.instrument(module, self)
