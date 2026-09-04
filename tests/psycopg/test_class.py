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
from wrapture import ConfigError, ConfigWarning, instrumentation

from wrapture_instrumentation_postgresql.psycopg import PsycopgInstrumentation


def test_class_data() -> None:
    assert PsycopgInstrumentation.target == "psycopg"
    assert PsycopgInstrumentation.removable is True
    assert PsycopgInstrumentation.requires == ()
    assert PsycopgInstrumentation.supports == ">=3.1,<4"

    assert set(PsycopgInstrumentation.settings) == {"statement"}
    assert PsycopgInstrumentation.settings["statement"].default is False


def test_the_description_is_the_docstring_first_line() -> None:
    assert (PsycopgInstrumentation.__doc__ or "").splitlines()[0] == (
        "Query and transaction tracing for psycopg (version 3)."
    )


def test_constructing_without_settings_works() -> None:
    instance = PsycopgInstrumentation()

    assert instance.settings == {"statement": False}
    assert instance.applied == ()
    assert instance.pending == ("psycopg",)


def test_an_undeclared_setting_is_refused() -> None:
    with pytest.raises(ConfigError, match="leaf"):
        PsycopgInstrumentation(leaf=False)


def test_a_setting_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ConfigError, match="statement"):
        PsycopgInstrumentation(statement="yes")


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
