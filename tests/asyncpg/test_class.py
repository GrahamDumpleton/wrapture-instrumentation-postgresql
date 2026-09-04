"""The class as wrapture reads it: its data, its settings, and the
installed asyncpg satisfying its supports range."""

from __future__ import annotations

import warnings
from importlib import metadata

import pytest

pytest.importorskip("asyncpg")

# asyncpg is imported for its side: the class's triggers fire on its
# import, so the applying test below works with this file run on its
# own.
import asyncpg  # noqa: F401
from wrapture import ConfigError, ConfigWarning, instrumentation

from wrapture_instrumentation_postgresql.asyncpg import AsyncpgInstrumentation

MODULES = ("asyncpg", "asyncpg.connection", "asyncpg.cursor", "asyncpg.prepared_stmt")


def test_class_data() -> None:
    assert AsyncpgInstrumentation.target == "asyncpg"
    assert AsyncpgInstrumentation.removable is True
    assert AsyncpgInstrumentation.requires == ()
    assert AsyncpgInstrumentation.supports == ">=0.29,<1"

    assert set(AsyncpgInstrumentation.settings) == {"statement"}
    assert AsyncpgInstrumentation.settings["statement"].default is False


def test_the_description_is_the_docstring_first_line() -> None:
    assert (AsyncpgInstrumentation.__doc__ or "").splitlines()[0] == (
        "Query and transaction tracing for asyncpg."
    )


def test_constructing_without_settings_works() -> None:
    instance = AsyncpgInstrumentation()

    assert instance.settings == {"statement": False}
    assert instance.applied == ()
    assert set(instance.pending) == set(MODULES)


def test_an_undeclared_setting_is_refused() -> None:
    with pytest.raises(ConfigError, match="leaf"):
        AsyncpgInstrumentation(leaf=False)


def test_a_setting_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ConfigError, match="statement"):
        AsyncpgInstrumentation(statement="yes")


def test_the_installed_asyncpg_is_within_supports() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)

        with instrumentation(AsyncpgInstrumentation) as record:
            (applied,) = record.instrumentations

            assert applied.target_version == metadata.version("asyncpg")
            assert set(applied.applied) == set(MODULES)
            assert applied.pending == ()
