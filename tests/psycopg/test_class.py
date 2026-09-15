"""The class as wrapture reads it: its data, its settings, and the
installed psycopg satisfying its supports range."""

from __future__ import annotations

import warnings
from importlib import metadata

import pytest

pytest.importorskip("psycopg")

# psycopg is imported for its side: the class's trigger fires on its
# import, so the applying test below works with this file run on its
# own.
import psycopg  # noqa: F401
from wrapture import Aspect, ConfigError, ConfigWarning, instrumentation

from wrapture_instrumentation_postgresql.psycopg import PsycopgInstrumentation


def test_class_data() -> None:
    assert PsycopgInstrumentation.target == "psycopg"
    assert PsycopgInstrumentation.removable is True
    assert PsycopgInstrumentation.requires == ()
    assert PsycopgInstrumentation.supports == ">=3.1,<4"

    settings = PsycopgInstrumentation.settings
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
    assert (PsycopgInstrumentation.__doc__ or "").splitlines()[0] == (
        "Query and transaction tracing for psycopg (version 3)."
    )


def test_constructing_without_settings_works() -> None:
    instance = PsycopgInstrumentation()

    statements = instance.settings["statements"]
    assert statements.enabled is True
    assert statements.options == {"leaf": True}
    assert statements.settings == {"statement": False}

    connections = instance.settings["connections"]
    assert connections.enabled is True
    assert connections.options == {"leaf": True}
    assert connections.settings == {}
    assert instance.applied == ()
    assert instance.pending == ("psycopg",)


def test_an_undeclared_setting_is_refused() -> None:
    with pytest.raises(ConfigError, match="verbosity"):
        PsycopgInstrumentation(verbosity=2)


def test_a_setting_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ConfigError, match="statement"):
        PsycopgInstrumentation(statement="yes")


def test_a_recording_key_under_an_aspect_is_checked() -> None:
    with pytest.raises(ConfigError, match="aspect 'statements': capture_result"):
        PsycopgInstrumentation(statements={"capture_result": "sumary"})


def test_an_unknown_key_under_an_aspect_is_refused() -> None:
    with pytest.raises(ConfigError, match="aspect 'connections': unknown keys"):
        PsycopgInstrumentation(connections={"verbosity": 2})


def test_a_flat_key_of_the_primary_aspect_reaches_it() -> None:
    instance = PsycopgInstrumentation(statement=True, leaf=False)

    statements = instance.settings["statements"]
    assert statements.settings == {"statement": True}
    assert statements.options == {"leaf": False}
    assert instance.settings["connections"].options == {"leaf": True}


def test_the_installed_psycopg_is_within_supports() -> None:
    # wrapture gates on supports before firing any trigger and warns,
    # never errors, when the version is outside it; make that warning
    # an error here so a matrix entry outside the range fails loudly
    # instead of passing with nothing applied.

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)

        with instrumentation(PsycopgInstrumentation) as record:
            (applied,) = record.instrumentations

            assert applied.target_version == metadata.version("psycopg")
            assert applied.applied == ("psycopg",)
            assert applied.pending == ()
