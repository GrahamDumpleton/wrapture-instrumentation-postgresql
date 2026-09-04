# wrapture-instrumentation-postgresql

Instrumentation for the PostgreSQL client libraries, applied through
[wrapture](https://github.com/GrahamDumpleton/wrapture).

wrapture attaches bindings to arbitrary Python call sites without
modifying the code being observed, and its config layer can switch on
packaged instrumentation for a third-party package by name. This is
the PostgreSQL package in that collection: one `wrapture.Instrumentation`
class per client library, so tracing every query, connection and
transaction your application sends to PostgreSQL is one config entry
and no code.

> **Status: alpha, ahead of 1.0.0.** Developed against wrapture's
> alpha series, with pre-releases published to
> [PyPI](https://pypi.org/project/wrapture-instrumentation-postgresql/),
> and until 1.0.0 is final a plain `pip install
> wrapture-instrumentation-postgresql` picks up the latest pre-release
> automatically, so there is no need to pin a specific version.

## Why a separate package

The core
[wrapture-instrumentation](https://github.com/GrahamDumpleton/wrapture-instrumentation)
package deliberately covers only the standard library and third-party
packages that can be exercised in-process, with no separate backend
product or service needed to test against. A PostgreSQL driver is
exactly the kind of target the separate-package rule was drawn for:
its tests need a real server, so this package's suite runs one in a
docker container, and it carries the drivers as test dependencies
(some of them compiled wheels) and its own release cadence, so the
core package's test matrix stays light. One package covers every
client library for the one backend: psycopg and psycopg2 now, and
asyncpg to follow.

## Installation

```console
$ pip install wrapture-instrumentation-postgresql
```

Installing it brings wrapture and nothing else. Neither driver is a
dependency: each instrumentation is inert until its driver is present,
and wrapture checks the installed version against the range the
instrumentation supports at apply time.

## Using it

An `[[instrument]]` entry in `wrapture.toml` names the target:

```toml
[[instrument]]
name = "psycopg"

[[sink]]
type = "printer"
```

and the runner applies it before the application starts, so the patch
is in place before the driver is imported:

```console
$ python -m wrapture -m myapp
```

The same config works through
[autowrapt](https://github.com/GrahamDumpleton/autowrapt) injection
(`AUTOWRAPT_BOOTSTRAP=wrapture python myapp.py`); through
[manual setup](https://wrapture.readthedocs.io/en/latest/manual-setup.html),
a few lines in the application's own startup where wrapping the launch
from outside is awkward; and, in a test, through
`wrapture.instrumentation("psycopg")` scoping the instrumentation to
a block. The
[ad-hoc tracing guide](https://wrapture.readthedocs.io/en/latest/ad-hoc-tracing.html)
covers the config file itself.

To see what is installed, what it supports in the current
environment, and what settings it takes:

```console
$ python -m wrapture.tools instrumentation --verbose
```

## Provided instrumentation

| Target | Supported versions | Records | Settings |
| ------ | ------------------ | ------- | -------- |
| [`psycopg`](https://github.com/GrahamDumpleton/wrapture-instrumentation-postgresql/blob/develop/src/wrapture_instrumentation_postgresql/psycopg/README.md) | psycopg 3.1+ (3.x) | Every query as one `database` leaf, however it was issued (a cursor's `execute` or `executemany`, the connection's shortcut, a streamed query, a COPY, a server-side cursor's DECLARE), plus the connection being opened and each transaction boundary (`commit`, `rollback`, the connection's context manager, and a `transaction()` block's begin and end, savepoints included); sync and async classes alike, and connections from a pool. Each event carries the system, the operation, and the database, host and port it reached; a failing statement records the driver's exception. The SQL text is recorded only with the `statement` setting on, bound parameters never. | `statement` |
| [`psycopg2`](https://github.com/GrahamDumpleton/wrapture-instrumentation-postgresql/blob/develop/src/wrapture_instrumentation_postgresql/psycopg2/README.md) | psycopg2 2.9+ (2.x), psycopg2-binary alike | Every query as one `database` leaf, however it was issued (`execute`, `executemany`, `callproc`, the extras' batch helpers, a named cursor's DECLARE), each COPY (`copy_from`, `copy_to`, `copy_expert`), the connection being opened and each transaction boundary (`commit`, `rollback`, the connection's context manager); through recording subclasses injected by psycopg2's own factory mechanism, so your `cursor_factory` and `connection_factory` classes keep working and are recorded too. Each event carries the system, the operation, and the database, host and port it reached; a failing statement records the driver's exception. The SQL text (the template with its placeholders) is recorded only with the `statement` setting on, bound parameters never. | `statement` |

The entry point name is the config's `name`"; the linked per-target
README is the full user documentation: what records, what the events
carry, the setting, and what is deliberately not traced.

## What is not traced

By design, and where it goes:

- Fetching rows: a query event closes when its execute returns, so
  time spent iterating rows afterwards is the application's, and a
  server-side cursor's FETCHes are not recorded (its DECLARE is).

- Pool checkouts (`psycopg_pool`): a connection taken from a pool
  records its queries like any other, but taking and returning it are
  not database operations and are not recorded.

- asyncpg: coming to this package as its own target.

- LISTEN/NOTIFY, large objects and two-phase commit are out of scope.

## Adding a target

Each client library is its own subpackage and entry point here. The
subpackage's `__init__.py` holds one `wrapture.Instrumentation`
subclass and imports only wrapture (and the package's own `common.py`,
which imports only wrapture too); everything that touches the driver
lives in sibling modules imported inside the hook. The class is
registered in `pyproject.toml` under
`[project.entry-points."wrapture.instrumentation"]`, and gets its own
test suite under `tests/<target>/` and a `README.md` linked from the
table above. The
[instrumentation packages](https://wrapture.readthedocs.io/en/latest/instrumentation-packages.html)
page of the wrapture documentation is the full contract; TESTING.md
here covers the tests and the server they run against.

## License

BSD 2-Clause. See
[LICENSE](https://github.com/GrahamDumpleton/wrapture-instrumentation-postgresql/blob/develop/LICENSE).
