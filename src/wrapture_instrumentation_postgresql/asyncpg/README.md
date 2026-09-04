# asyncpg instrumentation

Query and transaction tracing for
[asyncpg](https://magicstack.github.io/asyncpg/), the asyncio
PostgreSQL client. Entry point name `asyncpg`, the package it
patches; supports asyncpg 0.29 and later, below 1.0; fully
removable.

## Enabling it

An `[[instrument]]` entry in `wrapture.toml` (with at least one sink
to hear the events):

```toml
[[instrument]]
name = "asyncpg"

[[sink]]
type = "printer"
```

run under wrapture's runner (`python -m wrapture -m myapp`), or in a
test through the context manager:

```python
with wrapture.instrumentation("asyncpg"):
    ...
```

## What you see

One `database` leaf per operation, recorded around its await: the
connection being opened, each query through a connection's `execute`,
`executemany`, `fetch`, `fetchrow`, `fetchval` or `fetchmany`, each
of a prepared statement's own fetches, each round trip a server-side
cursor makes, each COPY, and each transaction boundary:

```
asyncpg:connect()  -> '<Connection>'
asyncpg.connection:Connection.execute(query='<6 chars>', args='<0 values>', timeout=None)  -> '<str>'
asyncpg.connection:Connection.fetch(query='<43 chars>', args='<1 values>', timeout=None, record_class=None)  -> '<list>'
asyncpg.prepared_stmt:PreparedStatement.fetchval(args='<1 values>', column=0, timeout=None)  -> '<int>'
```

- asyncpg's public surface is pure Python coroutine methods, so the
  instrumentation binds them in place on the classes and records
  each around its await; the Cython protocol beneath does the socket
  work and is where the seams stop. There is no callback-style
  completion to make spans overlap: one call, one span, closed when
  the await returns or raises. Two tasks querying at once give
  sibling spans that overlap in wall time, which is what they are.

- Every event carries the database contract keys `system`
  (`postgresql`) and `operation` (the SQL's leading keyword, or
  `CONNECT`, `DECLARE`, `FETCH`, `COPY`, `EXPLAIN`), which
  wrapture's OpenTelemetry export maps to `db.system.name` and
  `db.operation.name`, plus the `database`, `host` and `port` the
  connection reached. A COPY to or from a table names it as
  `collection`.

- Transactions need nothing of their own: `async with
  conn.transaction():` issues its BEGIN, COMMIT, ROLLBACK, SAVEPOINT,
  RELEASE SAVEPOINT and ROLLBACK TO through `Connection.execute`, so
  each boundary is an ordinary execute event with its own operation.

- A prepared statement (`await conn.prepare(query)`) records each of
  its `fetch`, `fetchrow`, `fetchval`, `fetchmany`, `executemany` and
  `explain` calls, with the statement's SQL; the `prepare` itself is
  not a query and is not recorded.

- A server-side cursor (`conn.cursor(query)` inside a transaction,
  iterated with `async for` or driven by `fetch`, `fetchrow` and
  `forward`) never passes through the connection's public methods,
  so its round trips are recorded where they happen: one `DECLARE`
  event when the cursor is bound (with the first batch of rows, for
  an iteration), one `FETCH` event per batch fetched, and a `MOVE`
  for a `forward`. The paths name asyncpg's private `BaseCursor`
  methods, since those are the round trips.

- The capture policy is deliberate about sensitive data: query
  arguments are never recorded, under any setting (they reduce to a
  count); the SQL text reduces to its length unless the `statement`
  setting is on; a COPY's records, source and output reduce to a
  count or a type; and the connect event captures none of its
  arguments, which carry the password. asyncpg sends arguments
  server-side, so the recorded text always carries `$1` placeholders
  rather than data, unless the application interpolated its own.

- A failing statement records the driver's exception
  (`asyncpg.UndefinedTableError`, say) on the event as it escapes; a
  timeout or cancellation arrives the same way. A pool made after the
  instrumentation is applied opens its connections through the
  bound `connect`, on the task that awaited it, so those record too.

## Settings

| Setting | Default | Controls |
| ------- | ------- | -------- |
| `statement` | `false` | Whether each query event records the SQL text as handed to the driver, as `statement`. Off by default because the driver cannot tell a literal an application interpolated from a placeholder; turn it on when your queries are parameterized, the text then carrying `$n` placeholders rather than data. Query arguments are never recorded either way. |

```toml
[[instrument]]
name = "asyncpg"
statement = true
```

## With the sqlalchemy instrumentation

An instrumented asyncpg beneath the core package's `sqlalchemy`
target (the `postgresql+asyncpg` async engine) composes through that
target's `leaf` setting. With the default `leaf = true` each
statement is one event and the driver's own events stay out of the
tree; with `leaf = false` they nest beneath each statement. The
asyncpg dialect prepares every statement and fetches through the
prepared statement, so what appears beneath `do_execute` is the
prepared statement's own fetch, and the commit's `execute` beneath
`_commit_impl`. Raw asyncpg use beside the engine records at the top
level either way, and the dialect's setup queries show up beside the
tree regardless, since they run outside the recorded seams.

## Known gap

A connection taken from a pool (`async with pool.acquire() as conn:`)
does not yet record its queries: asyncpg's pool proxy calls the
connection's methods through the class, `Connection.fetchval(
connection, query)`, a calling convention wrapture's signature check
does not yet handle. A fix is on wrapture's side and this note goes
when it lands. The pool's connects record, and so does everything on
a connection you opened yourself.

## How it patches

For the implementation detail see the module docstrings of
[connection.py](connection.py) (connect and the Connection methods),
[prepared.py](prepared.py) (prepared statements) and
[cursor.py](cursor.py) (server-side cursors).
