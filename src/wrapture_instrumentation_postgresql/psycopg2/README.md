# psycopg2 instrumentation

Query and transaction tracing for
[psycopg2](https://www.psycopg.org/docs/), the long-standing
PostgreSQL adapter. Entry point name `psycopg2`, the package it
patches (psycopg2-binary installs the same package); supports
psycopg2 2.9 and later, below 3; fully removable.

## Enabling it

An `[[instrument]]` entry in `wrapture.toml` (with at least one sink
to hear the events):

```toml
[[instrument]]
name = "psycopg2"

[[sink]]
type = "printer"
```

run under wrapture's runner (`python -m wrapture -m myapp`), or in a
test through the context manager:

```python
with wrapture.instrumentation("psycopg2"):
    ...
```

## What you see

One `database` leaf per operation: the connection being opened, each
query however it was issued (`execute`, `executemany`, `callproc`,
and the extras' batch helpers above them), each COPY (`copy_from`,
`copy_to`, `copy_expert`), and each transaction boundary (`commit`,
`rollback`, and the connection's commit-or-rollback context manager,
whose exit records which of the two it performed):

```
psycopg2:connect()  -> '<connection>'
psycopg2.extensions:cursor.execute(query='<31 chars>', vars='<1 values>')
psycopg2.extensions:connection.commit()
```

- `psycopg2.extensions.connection` and `.cursor` are C types no
  patch can touch, and psycopg2's own C entry points (`register_type`
  and everything built on it, `quote_ident`, `Json.prepare`,
  `lobject`) type-check the objects handed to them, so a proxy is
  ruled out too. The instrumentation instead uses the mechanism
  psycopg2 provides for its own extras: it binds `psycopg2.connect`
  to substitute the requested connection class with a recording
  subclass of it, and that subclass hands out cursors that are
  recording subclasses of whatever cursor class was asked for. Your
  own factories keep working and are simply recorded: a
  `cursor_factory` named at connect or per cursor (`DictCursor`,
  `RealDictCursor`, `NamedTupleCursor`), a `connection_factory` with
  a cursor default of its own (`RealDictConnection`,
  `LoggingConnection`), all of them real subclasses, so isinstance
  checks and the C type checks pass and reprs read as before.

- Every event carries the database contract keys `system`
  (`postgresql`) and `operation` (the SQL's leading keyword, or
  `CONNECT`, `COMMIT`, `ROLLBACK`, `CALL`, `COPY`), which wrapture's
  OpenTelemetry export maps to `db.system.name` and
  `db.operation.name`, plus the `database`, `host` and `port` the
  connection reached, from the connection's own info. A `callproc`
  names the procedure as `procedure`; a `copy_from` or `copy_to`
  names the table as `collection`.

- The connection's context manager (`with conn:`) commits, or rolls
  back when an exception is on its way through, and does not close
  the connection; its exit records which it did. A cursor's context
  manager only closes the cursor and records nothing.

- The capture policy is deliberate about sensitive data: bound
  parameters are never recorded, under any setting (they reduce to a
  count, or to a type name for a sequence the driver has yet to
  consume, which is never iterated); the SQL text reduces to its
  length unless the `statement` setting is on; a COPY's file reduces
  to its type; and the connect event captures none of its arguments,
  which carry the password. psycopg2 interpolates parameters
  client-side, below the seam, so the recorded text is always the
  template with its `%s` placeholders, never the query as sent.

- The extras' batch helpers (`execute_values`, `execute_batch`) call
  `execute` once per page or batch, and record one event each, which
  is what happens on the wire. A failing statement records the
  driver's exception (`psycopg2.errors.UndefinedTable`, say) on the
  event as it escapes. Fetching is not recorded: a query event closes
  when its execute returns, and a named (server-side) cursor's
  FETCHes go unrecorded for the same reason, its execute being the
  DECLARE.

- An asynchronous connection (`async_=True`) returns from `execute`
  before the query runs and the application polls; the event then
  measures the send only.

## Aspects

The instrumentation binds these aspects, each a group of call sites
with a switch, recording defaults and settings of its own:

| Aspect | Wraps | Records by default |
| ---- | ----- | ------------------ |
| `statements` (primary) | `execute`, `executemany`, `callproc`, `copy_from`, `copy_to` and `copy_expert` on every cursor, through the recording subclasses, as database events | `leaf = true`; SQL as its length and parameters as a count, an explicit capture key under the aspect replacing that |
| `connections` | `psycopg2.connect`, and `commit`, `rollback` and the context manager exit on every connection, as database events | `leaf = true`; the connect capturing nothing, the boundaries their (empty) arguments, an explicit capture key under the aspect replacing that |

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
name = "psycopg2"
statement = true
```

## With the sqlalchemy instrumentation

An instrumented psycopg2 beneath the core package's `sqlalchemy`
target composes through the `leaf` key on that target's aspects,
`statements` (flat on the entry) and `connections`. With the default
`leaf = true` each statement is one event and the driver's own events
stay out of the tree; with `leaf = false` the driver's events nest
beneath each statement: `cursor.execute` under `do_execute`, with
`leaf = false` under `connections` too `psycopg2:connect` under the
dialect's `connect`, and the driver's `executemany` (or the `execute`
calls of the dialect's `execute_values` batching, for compiled
inserts) under the psycopg2 dialect's own `do_executemany` override,
which the sqlalchemy target binds in its own right. Raw psycopg2 use
beside the engine records at the top level either way. A little of the
dialect's own housekeeping also shows up regardless, because it runs
straight against the driver outside the recorded seams: the settings
the dialect reads when it opens a connection, and the pool's
reset-on-return rollback.

## How it patches

For the implementation detail see the module docstring of
[factories.py](factories.py).
