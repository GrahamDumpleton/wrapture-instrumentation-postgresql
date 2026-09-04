"""Instrumentation for psycopg2: every query, the connections opened
and the transaction boundaries recorded as database events, through
recording subclasses of the driver's connection and cursor types
injected by way of the factory hooks psycopg2 itself provides.

This module imports only wrapture. Everything that touches psycopg2
lives in the sibling factories module, importing only wrapture at
top level and reaching psycopg2 through the package the hook is
handed or a lazy import inside a method, so loading this class when a
config loads never imports psycopg2 ahead of the hook meant to fire
on its import.
"""

from __future__ import annotations

from typing import Any

import wrapture
from wrapture import Setting

from . import factories


class Psycopg2Instrumentation(wrapture.Instrumentation):
    """Query and transaction tracing for psycopg2."""

    description = "Query and transaction tracing for psycopg2."

    target = "psycopg2"
    supports = ">=2.9,<3"
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

    @wrapture.instrumentation_hook("psycopg2")
    def psycopg2(self, name: str, module: Any) -> None:
        """Bind the connect factory and the recording classes' methods
        once psycopg2 exists."""

        factories.instrument(module, self)
