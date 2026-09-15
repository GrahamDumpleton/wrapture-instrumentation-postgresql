"""The class as wrapture reads it: its data, its settings, and the
installed psycopg2 satisfying its supports range."""

from __future__ import annotations

import warnings
from importlib import metadata

import pytest

pytest.importorskip("psycopg2")

# psycopg2 is imported for its side: the class's trigger fires on its
# import, so the applying test below works with this file run on its
# own.
import psycopg2  # noqa: F401
from wrapture import Aspect, ConfigError, ConfigWarning, instrumentation

from wrapture_instrumentation_postgresql.psycopg2 import Psycopg2Instrumentation


def test_class_data() -> None:
    assert Psycopg2Instrumentation.target == "psycopg2"
    assert Psycopg2Instrumentation.removable is True
    assert Psycopg2Instrumentation.requires == ()
    assert Psycopg2Instrumentation.supports == ">=2.9,<3"

    settings = Psycopg2Instrumentation.settings
    assert list(settings) == ["statements", "connections"]

    statements = settings["statements"]
    assert isinstance(statements, Aspect)
    assert statements.primary is True
    assert statements.defaults == {"leaf": True}
    assert set(statements.settings) == {"statement"}
    assert statements.settings["statement"].default is False

    connections = settings["connections"]
    assert isinstance(connections, Aspect)
    assert connections.primary is False
    assert connections.defaults == {"leaf": True}
    assert set(connections.settings) == set()


def test_the_description_is_the_docstring_first_line() -> None:
    assert (Psycopg2Instrumentation.__doc__ or "").splitlines()[0] == (
        "Query and transaction tracing for psycopg2."
    )


def test_constructing_without_settings_works() -> None:
    instance = Psycopg2Instrumentation()

    statements = instance.settings["statements"]
    assert statements.enabled is True
    assert statements.options == {"leaf": True}
    assert statements.settings == {"statement": False}

    connections = instance.settings["connections"]
    assert connections.enabled is True
    assert connections.options == {"leaf": True}
    assert connections.settings == {}
    assert instance.applied == ()
    assert instance.pending == ("psycopg2",)


def test_an_undeclared_setting_is_refused() -> None:
    with pytest.raises(ConfigError, match="verbosity"):
        Psycopg2Instrumentation(verbosity=2)


def test_a_setting_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ConfigError, match="statement"):
        Psycopg2Instrumentation(statement="yes")


def test_a_recording_key_under_an_aspect_is_checked() -> None:
    with pytest.raises(ConfigError, match="aspect 'statements': capture_result"):
        Psycopg2Instrumentation(statements={"capture_result": "sumary"})


def test_an_unknown_key_under_an_aspect_is_refused() -> None:
    with pytest.raises(ConfigError, match="aspect 'connections': unknown keys"):
        Psycopg2Instrumentation(connections={"verbosity": 2})


def test_a_flat_key_of_the_primary_aspect_reaches_it() -> None:
    instance = Psycopg2Instrumentation(statement=True, leaf=False)

    statements = instance.settings["statements"]
    assert statements.settings == {"statement": True}
    assert statements.options == {"leaf": False}
    assert instance.settings["connections"].options == {"leaf": True}


def test_the_installed_psycopg2_is_within_supports() -> None:
    # wrapture gates on supports before firing any trigger and warns,
    # never errors, when the version is outside it; make that warning
    # an error here so a matrix entry outside the range fails loudly
    # instead of passing with nothing applied.

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)

        with instrumentation(Psycopg2Instrumentation) as record:
            (applied,) = record.instrumentations

            # psycopg2-binary installs the psycopg2 import package but
            # its distribution is its own; wrapture reads whichever is
            # present.

            assert applied.target_version in (
                metadata.version("psycopg2-binary"),
                metadata.version("psycopg2") if _installed("psycopg2") else None,
            )
            assert applied.applied == ("psycopg2",)
            assert applied.pending == ()


def _installed(distribution: str) -> bool:
    try:
        metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return False
    return True
