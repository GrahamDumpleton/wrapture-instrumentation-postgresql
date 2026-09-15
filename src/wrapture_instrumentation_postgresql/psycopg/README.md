# psycopg instrumentation

Query and transaction tracing for
[psycopg](https://www.psycopg.org/psycopg3/) (version 3), the current
PostgreSQL adapter. Entry point name `psycopg`, the package it
patches; supports psycopg 3.1 and later, below 4; fully removable.
Sync and async classes alike, whichever of the pure Python, C or
binary implementations is installed.

## Enabling it

An `[[instrument]]` entry in `wrapture.toml` (with at least one sink
to hear the events):

```toml
[[instrument]]
name = "psycopg"

[[sink]]
type = "printer"
```

run under wrapture's runner (`python -m wrapture -m myapp`), or in a
test through the context manager:

```python
with wrapture.instrumentation("psycopg"):
    ...
```

## What you see

One `database` leaf per operation: the connection being opened, each
query however it was issued (a cursor's `execute` or `executemany`,
or the connection's `execute` shortcut), each streamed query, each
COPY, and each transaction boundary (`commit`, `rollback`, the
connection's commit-or-rollback context manager, and a
`transaction()` block's begin and end):

```
psycopg:connect()  -> '<Connection>'
psycopg:Cursor.execute(query='<29 chars>', params='<1 values>', prepare=None, binary=None)  -> '<Cursor>'
psycopg:Transaction.__enter__()  -> '<Transaction>'
psycopg:Transaction.__exit__(exc_type=None, exc_val=None, exc_tb=None)  -> '<bool>'
psycopg:Connection.commit()
```

- psycopg's classes are pure Python, so the instrumentation binds
  their methods in place: `Cursor.execute` and `executemany` (which
  every cursor class inherits, `ClientCursor` and `RawCursor`
  included, and which the connection's `execute` shortcut calls, so
  it records once), `Cursor.stream`, `Cursor.copy`, the server-side
  cursors' own `execute` (a DECLARE), `Connection.connect` (and the
  `psycopg.connect` spelling of it), `commit`, `rollback`, the
  connection's context manager exit, and the `transaction()` block's
  enter and exit. The async classes are bound the same way and
  record around the await. Connections from a `psycopg_pool` pool
  record like any other, since the bindings sit on the classes.

- Every event carries the database contract keys `system`
  (`postgresql`) and `operation` (the SQL's leading keyword, or
  `CONNECT`, `COMMIT`, `ROLLBACK`, `BEGIN`, `SAVEPOINT`, `RELEASE`,
  `DECLARE`, `COPY`), which wrapture's OpenTelemetry export maps to
  `db.system.name` and `db.operation.name`, plus the `database`,
  `host` and `port` the connection reached, from the connection's
  own info, so every span says which server it went to.

- A `transaction()` block records what it did: `BEGIN` on entering
  when nothing was open on the connection, `SAVEPOINT` (with the
  savepoint name) for a nested block, and on leaving `COMMIT` or
  `RELEASE`, or `ROLLBACK` when an exception passed through,
  `force_rollback` was set or `psycopg.Rollback` was raised inside.
  The connection's own context manager records its `COMMIT` or
  `ROLLBACK` the same way. The `BEGIN` psycopg sends implicitly
  before the first statement of a transaction goes with that
  statement and folds into its event.

- A streamed query (`cursor.stream()`) records one event around the
  whole iteration: its duration is the time spent inside the
  generator and `items` the rows streamed; the event closes when the
  iteration ends or the generator is abandoned.

- A COPY (`with cursor.copy(...) as copy:`) records one block event
  spanning the transfer, from entering the block to leaving it,
  labelled `psycopg:Cursor.copy` (or `AsyncCursor.copy`), with the
  operation `COPY` and the rows copied as `rows`.

- The capture policy is deliberate about sensitive data: bound
  parameters are never recorded, under any setting (they reduce to a
  count, or to a type name for a parameter sequence the driver has
  yet to consume, which is never iterated); the SQL text reduces to
  its length unless the `statement` setting is on; and the connect
  event captures none of its arguments, which carry the password.
  There is no obfuscation at this layer, because rewriting SQL to
  strip literals is a losing game outside a real lexer: record the
  text where queries are parameterized, leave it off where they are
  not.

- A failing statement records the driver's exception
  (`psycopg.errors.UndefinedTable`, say) on the event as it escapes.
  Fetching is not recorded: a query event closes when its execute
  returns, so time spent iterating rows afterwards is not attributed
  to the database, and a server-side cursor's FETCHes from its portal
  go unrecorded for the same reason, its DECLARE being the event.

- In pipeline mode (`with conn.pipeline():`) an execute returns as
  soon as the query is queued and the results arrive at the sync
  point, so the statement events are short and the wait sits in the
  pipeline's exit; every statement still records.

## Aspects

The instrumentation binds these aspects, each a group of call sites
with a switch, recording defaults and settings of its own:

| Aspect | Wraps | Records by default |
| ---- | ----- | ------------------ |
| `statements` (primary) | `execute` and `executemany` on `Cursor` and `AsyncCursor` (and so the connection's `execute` shortcut), the server-side cursors' `execute`, `stream`, and the `copy` block, as database events | `leaf = true`; SQL as its length and parameters as a count, an explicit capture key under the aspect replacing that |
| `connections` | `connect` in its three spellings, `commit`, `rollback` and the context manager exit on both connection classes, and the enter and exit of `transaction()` blocks, as database events | `leaf = true`; the connect capturing nothing, the boundaries their (empty) arguments, an explicit capture key under the aspect replacing that |

An aspect is addressed as a sub-table of the entry,
`[instrument.connections]`, and takes the recording keys an
`[[observe]]` entry does (`capture`, `capture_args`, `capture_result`,
`redact`, `redact_result`, `redact_marker`, `leaf` and `stack`) beside
the settings listed below, so `capture_result = "types"` or
`redact = ["token"]` under an aspect means exactly what it means on an
observe entry. `enabled = false` under an aspect switches it off, and
a bare `connections = false` on the entry means the same. The primary
aspect's keys may be written flat on the entry. Each aspect is a leaf
by default, which is what keeps one application call to one event;
`leaf = false` under one exposes whatever the driver's inner calls
record beneath it. The [aspects
section](https://wrapture.readthedocs.io/en/latest/instrumentation-packages.html#aspects)
of the wrapture documentation has the whole scheme.

## Settings

| Setting | Aspect | Default | Controls |
| ------- | ---- | ------- | -------- |
| `statement` | `statements` | `false` | Whether each query event records the SQL text as handed to the driver, as `statement` (a composed `sql.SQL(...)` query rendered as it will be sent). Off by default because the driver cannot tell a literal an application interpolated from a placeholder; turn it on when your queries are parameterized, the text then carrying placeholders rather than data. A `sql.Literal` composed into a query is recorded as written, so prefer placeholders there too. Bound parameters are never recorded either way. |

```toml
[[instrument]]
name = "psycopg"
statement = true
```

## With the sqlalchemy instrumentation

An instrumented psycopg beneath the core package's `sqlalchemy` target
composes through the `leaf` key on that target's aspects, `statements`
(flat on the entry) and `connections`. With the default `leaf = true`
each statement is one event and the driver's own events stay out of
the tree; with `leaf = false` the driver's events nest beneath each
statement, `psycopg:Cursor.execute` under `do_execute`, with
`leaf = false` under `connections` too `psycopg:connect` under the
dialect's `connect`, the async engine's `AsyncCursor.execute`
likewise. Raw psycopg use beside the engine records at the top level
either way. A little of the dialect's own housekeeping also shows up
regardless, because it runs straight against the driver outside the
recorded seams: the type lookups the psycopg dialect makes when it
opens a connection, and the pool's reset-on-return rollback.

## How it patches

For the implementation detail see the module docstrings of
[cursor.py](cursor.py) (the execute family, stream and COPY),
[connection.py](connection.py) (connect and the connection's own
boundaries) and [transaction.py](transaction.py) (the `transaction()`
block).
