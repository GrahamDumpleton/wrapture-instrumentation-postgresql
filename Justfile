# Supported Python versions. No free threaded builds: the
# instrumentation is pure Python and behaves the same on them, and a
# free threaded row would only be testing whether the drivers' own C
# extensions build without the GIL, which is their concern. The full
# list runs inside docker (test-all), since 3.15 has no psycopg-binary
# wheel yet and psycopg's pure Python implementation then needs a
# libpq, which the image supplies and the machine need not.
python_versions := "3.12 3.13 3.14 3.15"

# One representative psycopg release per supported minor to run its
# suite against; the instrumentation's `supports` range is kept honest
# by these. The lock's own latest is covered by the plain `test` runs,
# so it is not repeated here.
psycopg_versions := "3.1.20 3.2.10"
psycopg2_versions := "2.9.9"

# Where the compose file publishes the server for postgresql-start.
postgresql_port := "54329"

# List available targets.
default:
    @just --list

# Starts a throwaway PostgreSQL container itself unless
# WRAPTURE_POSTGRESQL_URL points at a server already running.
# Run the test suite on the default Python version; extra args go to pytest.
test *ARGS:
    uv run pytest {{ARGS}}

# Run one target's test suite, e.g. `just test-target psycopg`.
test-target TARGET *ARGS:
    uv run pytest tests/{{TARGET}} {{ARGS}}

# The wrapture[otel] overlay carries the optional OpenTelemetry
# dependencies, and WRAPTURE_OTEL makes the session fixture in
# tests/conftest.py install the OpenTelemetry sink, so everything the
# tests record exports as spans to a local OTLP endpoint
# (localhost:4318 unless OTEL_EXPORTER_OTLP_ENDPOINT says otherwise),
# for checking the spans in a backend such as otel-desktop-viewer.
# Run the test suite with the events also exported as OpenTelemetry spans.
test-otel *ARGS:
    WRAPTURE_OTEL=1 uv run --with "wrapture[otel]" pytest {{ARGS}}

# Each version gets its own environment so the default .venv is
# untouched, and only the test dependency group is installed. 3.15
# needs a libpq on the machine this way; prefer `just test-docker
# 3.15`, which does not.
# Run the test suite natively on one nominated version, e.g. `just test-python 3.13`.
test-python VERSION *ARGS:
    UV_PROJECT_ENVIRONMENT=.venv-{{VERSION}} uv run --python {{VERSION}} --no-default-groups --group test pytest {{ARGS}}

# Against the compose file's own PostgreSQL service; extra args go to
# pytest. The first run per version fetches the interpreter and
# installs the test group into a volume; reruns reuse both.
# Run the test suite inside docker on one nominated version, e.g. `just test-docker 3.15`.
test-docker VERSION *ARGS:
    UV_PYTHON={{VERSION}} docker compose run --rm --build tests {{ARGS}}

# Run the test suite inside docker on every supported Python version.
test-docker-all *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    for version in {{python_versions}}; do
        echo "=== Python ${version} (docker) ==="
        just test-docker "${version}" {{ARGS}}
    done

# Run the test suite on every supported Python version (in docker).
test-all *ARGS:
    just test-docker-all {{ARGS}}

# The psycopg suite against one psycopg version, in an environment of
# its own on Python 3.12: psycopg's binary wheel must match the
# psycopg version, and the older lines ship binary wheels only up to
# the Pythons of their day, so the overlay asks for psycopg[binary] at
# that version on a Python every line has a wheel for, and no libpq
# is needed from the machine. The lock's own version is covered by the
# plain `test` runs.
# Run the psycopg suite against one psycopg version, e.g. `just test-psycopg 3.1.20`.
test-psycopg VERSION *ARGS:
    UV_PROJECT_ENVIRONMENT=.venv-3.12 uv run --python 3.12 --no-default-groups --group test --with "psycopg[binary]=={{VERSION}}" pytest tests/psycopg {{ARGS}}

# Run the psycopg suite against every version in psycopg_versions.
test-psycopg-all *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    for version in {{psycopg_versions}}; do
        echo "=== psycopg ${version} ==="
        just test-psycopg "${version}" {{ARGS}}
    done

# The psycopg2 suite against one psycopg2 line, likewise on Python
# 3.12 with psycopg2-binary at that version (the older line's wheels
# stop at 3.12).
# Run the psycopg2 suite against one psycopg2 version, e.g. `just test-psycopg2 2.9.9`.
test-psycopg2 VERSION *ARGS:
    UV_PROJECT_ENVIRONMENT=.venv-3.12 uv run --python 3.12 --no-default-groups --group test --with "psycopg2-binary=={{VERSION}}" pytest tests/psycopg2 {{ARGS}}

# Run the psycopg2 suite against every version in psycopg2_versions.
test-psycopg2-all *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    for version in {{psycopg2_versions}}; do
        echo "=== psycopg2 ${version} ==="
        just test-psycopg2 "${version}" {{ARGS}}
    done

# Published on localhost; prints the URL to export for the demos or
# for running the tests natively against it rather than a throwaway
# container per session.
# Start the compose file's PostgreSQL server alone.
postgresql-start:
    WRAPTURE_POSTGRESQL_PORT={{postgresql_port}} docker compose up -d --wait postgres
    @echo "export WRAPTURE_POSTGRESQL_URL=postgresql://postgres:postgres@localhost:{{postgresql_port}}/postgres"

# Stop the compose file's PostgreSQL server (and the tests container).
postgresql-stop:
    docker compose down

# The test fixture names its throwaway containers
# wrapture-postgresql-<pid> and removes them itself; an interrupted
# run can leave one behind.
# Remove any throwaway server container an interrupted test run left behind.
postgresql-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    containers="$(docker ps -aq --filter name=wrapture-postgresql-)"
    if [ -n "${containers}" ]; then
        docker rm -f ${containers}
    fi

# A table, inserts, a transaction block with a rollback, a COPY, a
# streamed query and a failing statement, with and without SQL text
# recording; the live event stream, then the reconstructed tree. The
# wrapture[otel] overlay carries the optional OpenTelemetry
# dependencies, so `just demo-psycopg --otel` also exports the events
# as spans to a local OTLP endpoint (localhost:4318 unless
# OTEL_EXPORTER_OTLP_ENDPOINT says otherwise).
# Drive psycopg against the server WRAPTURE_POSTGRESQL_URL names with the instrumentation applied.
demo-psycopg *ARGS:
    uv run --with "wrapture[otel]" python -m demo.psycopg {{ARGS}}

# The same shapes through psycopg2: a table, inserts (one through
# execute_values), a COPY, a rollback, a failing statement, with and
# without SQL text recording; `--otel` as for demo-psycopg.
# Drive psycopg2 against the server WRAPTURE_POSTGRESQL_URL names with the instrumentation applied.
demo-psycopg2 *ARGS:
    uv run --with "wrapture[otel]" python -m demo.psycopg2 {{ARGS}}

# The package depends on a released wrapture. This overlays a checkout
# of wrapture from the sibling directory as an editable install for the
# run, for iterating against unreleased wrapture changes without
# touching pyproject.toml.
# Run the test suite against the wrapture checkout in ../wrapture.
test-dev *ARGS:
    uv run --with-editable ../wrapture pytest {{ARGS}}

# Check code with the ruff linter and formatter.
lint:
    uv run ruff check src tests demo
    uv run ruff format --check src tests demo

# Reformat code and fix lint issues that are auto-fixable.
format:
    uv run ruff format src tests demo
    uv run ruff check --fix src tests demo

# Type check the project with mypy.
typecheck:
    uv run mypy

# Build the source distribution and wheel into dist/.
build:
    uv build

# Remove temporary files: caches, virtual environments and build artifacts.
clean:
    rm -rf .venv .venv-*
    rm -rf build dist src/*.egg-info *.egg-info
    rm -rf .pytest_cache .mypy_cache .ruff_cache
    find . -type d -name __pycache__ -not -path "./scratch/*" -exec rm -rf {} +
