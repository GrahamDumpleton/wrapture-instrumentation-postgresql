"""Fixtures for the psycopg suite: the instrumentation applied and a
scoped tape, over the shared server fixture."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# psycopg is the suite's own test dependency; a build without it
# skips rather than errors.
pytest.importorskip("psycopg")

from wrapture import Tape, instrumentation, timeline

from wrapture_instrumentation_postgresql.psycopg import PsycopgInstrumentation


@pytest.fixture
def tape() -> Iterator[Tape]:
    with instrumentation(PsycopgInstrumentation), timeline() as recorded:
        yield recorded
