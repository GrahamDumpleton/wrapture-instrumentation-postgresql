# Testing

## Where the tests are

Tests live in the [tests/](tests/) directory at the top of the
repository, separate from the package code in
src/wrapture_instrumentation_postgresql/. Test files are named
`test_*.py` and are discovered by pytest, configured via the
`[tool.pytest.ini_options]` section of [pyproject.toml](pyproject.toml).

The directory has two levels:

- Package-level tests directly under tests/: the version, the rule
  that importing the package or loading any registered class never
  imports a target, and the listing tool reporting every entry
  cleanly.

- One subdirectory per target, `tests/<target>/` (`tests/psycopg/`,
  `tests/psycopg2/`),
  holding that instrumentation's suite: settings validation, applying
  and removing the class directly, the whole path through
  `wrapture.instrumentation()` with a timeline recording what the
  bindings observe (sync and async), resolving the entry point by
  name, a check that the installed driver satisfies the class's
  `supports` range, and the composition tests with the core package's
  sqlalchemy target.

Shared helpers and the server fixture live in
[tests/conftest.py](tests/conftest.py).

## The PostgreSQL server

The suites drive the real drivers against a real PostgreSQL server;
nothing is mocked. The session-scoped `postgresql` fixture supplies
it, in one of two ways:

- `WRAPTURE_POSTGRESQL_URL` in the environment names a server already
  running (`postgresql://user:password@host:port/dbname`), and the
  fixture uses it and starts nothing. This is how CI's service
  container, the compose file's `postgres` service and a server of
  your own are used.

- Otherwise the fixture runs a throwaway container from the official
  `postgres:17` image on a random localhost port, waits until a real
  connection from the host answers, and removes the container at the
  end of the session. This needs the docker CLI and a running daemon
  (Docker Desktop, or anything docker-compatible).

With neither, the session fails with a message saying so rather than
skipping, so a green run always means the suites ran. The fixture
yields a `Server` with the parts in both spellings the drivers take
(`server.url`, `server.kwargs`). Tests isolate themselves with
temporary tables (`CREATE TEMP TABLE`), private to their connection
and gone when it closes, so no cleanup fixtures are needed.

An interrupted run can leave its throwaway container behind;
`just postgresql-clean` removes any such container.

## Running the tests

All tooling in this project goes through
[uv](https://docs.astral.sh/uv/), which manages the project
environment and installs the package, its development dependencies
(including pytest) and the drivers the tests need.

The simplest way to run the test suite is via the Justfile target,
which runs natively on the default Python version and starts a
throwaway server unless `WRAPTURE_POSTGRESQL_URL` is set:

```console
just test
```

Extra arguments are passed through to pytest, for example:

```console
just test -v
just test tests/psycopg/test_recording.py
just test -k transaction
```

One target's suite alone:

```console
just test-target psycopg
```

Equivalently, run pytest directly with uv:

```console
uv run pytest
```

To run against a longer-lived server instead of a throwaway one per
session, start the compose file's server and export the URL it
prints:

```console
just postgresql-start
export WRAPTURE_POSTGRESQL_URL=postgresql://postgres:postgres@localhost:54329/postgres
just test
just postgresql-stop
```

## Watching what the tests record

The suites assert on tapes rather than printing anything, but the
recorded events can be watched live for visual verification. Setting
WRAPTURE_PRINTER in the environment installs a process-wide
wrapture.Printer sink for the session, streaming one line to stderr as
each operation begins and a closing line with its outcome and timing;
pytest captures stderr, so add -s to see it:

```console
WRAPTURE_PRINTER=1 just test tests/psycopg -s
```

The same events can go to an OpenTelemetry backend instead, for
checking the spans, their kinds and their attributes in something
like otel-desktop-viewer. Setting WRAPTURE_OTEL installs wrapture's
OpenTelemetry sink for the session (exporting over OTLP to
http://localhost:4318, or wherever OTEL_EXPORTER_OTLP_ENDPOINT
points) and flushes it when the session ends; the `test-otel` recipe
sets the variable and overlays the wrapture[otel] dependencies the
sink needs:

```console
just test-otel tests/psycopg/test_recording.py -k transaction
```

Each event the tests record arrives as its own single-span trace:
the tests drive the driver directly with nothing of their own around
the calls, and a `timeline()` roots what it records (an enclosing
block or observed function outside the timeline is not the parent of
what happens inside it), so a root span per test would have to be
opened inside each test's own timeline and would then appear on the
tape the assertions read.

For a purpose-built run rather than the tests' traffic, the demo
module under demo/ applies the instrumentation, drives the driver
against the server `WRAPTURE_POSTGRESQL_URL` names, and prints both
the live stream and the reconstructed tree with timings:

```console
just postgresql-start
export WRAPTURE_POSTGRESQL_URL=postgresql://postgres:postgres@localhost:54329/postgres
just demo-psycopg
just demo-psycopg2
```

With --otel the same events also export as OpenTelemetry spans over
OTLP (to http://localhost:4318, or wherever
OTEL_EXPORTER_OTLP_ENDPOINT points), for verifying the spans in a
local backend such as Jaeger; the Justfile target overlays the
wrapture[otel] dependencies for the run:

```console
just demo-psycopg --otel
```

## Testing across Python versions

The project supports Python 3.12 through 3.15. Free threaded builds
are not in the matrix: the instrumentation is pure Python and behaves
the same on them, and a free threaded row would only be testing
whether the drivers' own C extensions build without the GIL, which is
their concern. The supported list is defined at the top of the
[Justfile](Justfile); the default version used by plain `just test`
is pinned in [.python-version](.python-version).

The full matrix runs inside docker, so the machine never needs
PostgreSQL client libraries: psycopg's binary wheels bundle a libpq up
to Python 3.14, but on 3.15 psycopg runs as pure Python and needs a
libpq from the machine, which the image in the [Dockerfile](Dockerfile)
supplies. The [compose.yml](compose.yml) file defines the server and
a `tests` container built from that image; uv fetches the requested
interpreter inside the container and installs the test dependencies
into a per-version environment, both kept on named volumes so reruns
pay for neither.

```console
just test-all
just test-docker 3.15
```

The first run per version downloads the interpreter and the wheels.
For the versions where every driver has a wheel, the suite can also
run natively in a per-version environment (.venv-VERSION) that leaves
the default .venv untouched:

```console
just test-python 3.13
```

## Testing across driver versions

The `test` dependency group installs each driver at whatever version
the lock resolves. The instrumentation's `supports` range is kept
honest by running its suite against other lines of the driver. Each
line has a place in `psycopg_versions` or `psycopg2_versions` in the
Justfile, run one at a time by `just test-psycopg 3.1.20` or
`just test-psycopg2 2.9.9`, or all by the `-all` forms, and the CI
workflow runs the same matrix. These runs use an environment of their
own on Python 3.12 with the driver's binary distribution at the
requested version (`psycopg[binary]`, `psycopg2-binary`): a driver's
binary wheel must match its version, and the older lines ship binary
wheels only up to the Pythons of their day, so 3.12 is the Python
every line has a wheel for, and no libpq is needed from the machine. A test in the suite
asserts the installed driver satisfies `supports`, so a matrix entry
outside the range fails loudly rather than passing vacuously.

## Continuous integration

The workflow runs every test job on Linux with a PostgreSQL service
container (service containers are Linux-only, and the drivers do not
differ per OS at the instrumented layer), the URL in the environment,
across Python 3.12 to 3.15 and the driver version matrix.

## Testing against unreleased wrapture

The package depends on a released wrapture. To run the tests against a
checkout of wrapture in the sibling directory ../wrapture, without
editing pyproject.toml:

```console
just test-dev
```

which overlays that checkout as an editable install for the run.

## Writing tests

- Put new test files in tests/ (package level) or tests/<target>/
  (for one instrumentation) and name them `test_*.py`.

- Import the package under test as
  `wrapture_instrumentation_postgresql`, and the instrumentation
  classes from their subpackages. The project is installed into the
  uv-managed environment, so no path manipulation is needed.

- Guard the driver imports with `pytest.importorskip` at the top of
  the module (`pytest.importorskip("psycopg")`), so a build without
  that driver skips the suite rather than erroring.

- Take the `postgresql` fixture for the server and create temporary
  tables for whatever the test needs; never leave ordinary tables
  behind.

- Validate behaviour with wrapture's own unit testing layer:
  `wrapture.timeline()` to record what the instrumentation's bindings
  observe and the tape's tree and queries to assert on it, and
  `wrapture.instrumentation()` to scope an application of a class to
  a block, driving the real driver against the real server. Do not
  mock a driver and do not use `unittest.mock`. If something cannot
  be expressed that way, write it plainly with a comment naming the
  gap, and say so when summarising the work.

- Async cases run their coroutine with `asyncio.run` inside the test;
  no pytest-asyncio.

- Tests should not depend on anything in the scratch/ directory,
  which is not part of the repository.
