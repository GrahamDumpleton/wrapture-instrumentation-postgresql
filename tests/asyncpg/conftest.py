"""Fixtures for the asyncpg suite: the instrumentation applied and a
scoped tape, over the shared server fixture."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# asyncpg is the suite's own test dependency; a build without it (one
# where asyncpg has no wheel yet) skips rather than errors.
pytest.importorskip("asyncpg")

from wrapture import Tape, instrumentation, timeline

from wrapture_instrumentation_postgresql.asyncpg import AsyncpgInstrumentation


@pytest.fixture
def tape() -> Iterator[Tape]:
    with instrumentation(AsyncpgInstrumentation), timeline() as recorded:
        yield recorded
