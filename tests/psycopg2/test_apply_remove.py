"""Applying and removing: the patched names on the module and on the
package's own recording mixins, and that removal leaves them all as
they were whatever the setting, with connections made meanwhile
still working."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg2")

import psycopg2
import psycopg2.extensions
from wrapture import instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_postgresql.psycopg2 import (
    Psycopg2Instrumentation,
    factories,
)


def choke_points() -> dict[str, object]:
    """The callables currently at every patched name."""

    return {
        "connect": psycopg2.connect,
        "execute": factories.CursorMixin.execute,
        "executemany": factories.CursorMixin.executemany,
        "callproc": factories.CursorMixin.callproc,
        "copy_from": factories.CursorMixin.copy_from,
        "copy_to": factories.CursorMixin.copy_to,
        "copy_expert": factories.CursorMixin.copy_expert,
        "commit": factories.ConnectionMixin.commit,
        "rollback": factories.ConnectionMixin.rollback,
        "exit": factories.ConnectionMixin.__exit__,
    }


@pytest.mark.parametrize("statement", [False, True])
def test_apply_then_remove_leaves_everything_as_it_was(statement: bool) -> None:
    # The statement setting shapes the recorded data, not the patch,
    # so the patched set is the same either way.

    before = choke_points()

    with instrumentation(Psycopg2Instrumentation, statement=statement) as record:
        (instance,) = record.instrumentations

        assert instance.applied == ("psycopg2",)

        current = choke_points()
        for name in before:
            assert current[name] is not before[name], name

    current = choke_points()
    for name in before:
        assert current[name] is before[name], name

    assert not instance.applied


def test_after_removal_connections_come_back_bare(postgresql: Server) -> None:
    with instrumentation(Psycopg2Instrumentation):
        recording = psycopg2.connect(postgresql.url)
        recording.close()
        assert isinstance(recording, factories.ConnectionMixin)

    bare = psycopg2.connect(postgresql.url)
    try:
        assert type(bare) is psycopg2.extensions.connection
        assert type(bare.cursor()) is psycopg2.extensions.cursor
    finally:
        bare.close()


def test_a_connection_made_while_applied_keeps_working_after_removal(
    postgresql: Server,
) -> None:
    # The recording classes stay on the objects already made; their
    # methods then delegate without recording.

    with instrumentation(Psycopg2Instrumentation):
        connection = psycopg2.connect(postgresql.url)

    try:
        with timeline() as tape:
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
            connection.commit()

        assert tape.all == []
    finally:
        connection.close()
