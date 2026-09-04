"""Fixtures for the psycopg2 suite: the instrumentation applied and a
scoped tape, over the shared server fixture."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# psycopg2 is the suite's own test dependency; a build without it
# (one where psycopg2-binary has no wheel yet) skips rather than
# errors.
pytest.importorskip("psycopg2")

from wrapture import Tape, instrumentation, timeline

from wrapture_instrumentation_postgresql.psycopg2 import Psycopg2Instrumentation


@pytest.fixture
def tape() -> Iterator[Tape]:
    with instrumentation(Psycopg2Instrumentation), timeline() as recorded:
        yield recorded
