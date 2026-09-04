"""The psycopg2 patches: the connect factory bound, and the
connections it returns made instances of recording subclasses, their
cursors likewise.

psycopg2's connection and cursor are C types, so there is no
attribute on them a binding could patch, and a proxy around them is
not an option either: psycopg2's own C entry points type-check the
objects handed back to them (`register_type`, and so `register_uuid`,
`register_hstore`, `register_json` and `register_composite`, plus
`quote_ident`, `Json(...).prepare()` and `lobject`), and a proxy
fails those checks. What psycopg2 does support is subclassing: the
`connection_factory=` argument to `connect()` and the `cursor_factory`
of a connection or a `cursor()` call are how its own extras
(`LoggingConnection`, `DictCursor`) work. The instrumentation uses
that mechanism: the one seam is `psycopg2.connect`, bound to record
the open and to substitute the requested connection factory with a
recording subclass of it, and that subclass hands out cursors that
are recording subclasses of whatever cursor class was asked for.

The recording subclasses are made per base class and cached: a mixin
of plain Python methods (`CursorMixin`, `ConnectionMixin`) placed
ahead of the base in the bases, each override calling the base's
method through super(), so the application's own factories
(`RealDictConnection`, `DictCursor`, `LoggingConnection`) keep
working and are simply recorded. The subclass takes the base's name
and module, so reprs read as before; isinstance checks and the C
type checks pass, since these are real subclasses. Those mixin
methods are what wrapture binds, each labelled with the psycopg2 name
it stands for (`psycopg2.extensions:cursor.execute`), the one thing
its recorded path cannot say.

Cursors are made recording in `ConnectionMixin.cursor`. A cursor class
named explicitly, in the call or on the connection's `cursor_factory`,
is substituted with its recording subclass before the base builds
the cursor. When nothing names one and the base would fall back to
psycopg2's own C cursor type, the recording subclass of that type is
passed explicitly, since an instance of the C type itself cannot be
reclassed afterwards. When a base class's own `cursor()` supplies a
default (the extras' connection classes do), it runs first and the
cursor it returns, an instance of a Python subclass, is reclassed to
the recording subclass of its class on the way out, which Python
permits for compatible heap types.

The recorded set is the acquisition, the execute family and the
transaction boundaries: `connect`; `execute`, `executemany` and
`callproc`; `copy_from`, `copy_to` and `copy_expert`; `commit` and
`rollback`; and the connection's commit-or-rollback context manager,
whose exit records which of the two it performed (psycopg2's does
not close the connection). Cursor creation and the fetch methods are
not recorded, and a named cursor's FETCHes not either, the model
every database target here follows. Every event carries the
database contract keys and the server reached, from the connection's
info; the SQL text as the application handed it over (psycopg2
interpolates the parameters client-side below this seam, so the
recorded text carries the placeholders) rides as `statement` only
when the setting is on, and bound parameters are never recorded.

Removing the instrumentation removes the bindings and restores
`connect`: connections already made keep their recording classes,
whose methods then delegate without recording.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, cast

import wrapture

from ..common import SYSTEM, captured, query_data, server_of

_connections: dict[type, type] = {}
_cursors: dict[type, type] = {}


class CursorMixin:
    """The recording overrides placed ahead of a psycopg2 cursor class:
    each calls the base's method through super(), and each is bound
    by wrapture below."""

    def execute(self, query: Any, vars: Any = None) -> Any:
        return cast(Any, super()).execute(query, vars)

    def executemany(self, query: Any, vars_list: Any) -> Any:
        return cast(Any, super()).executemany(query, vars_list)

    def callproc(self, procname: Any, parameters: Any = None) -> Any:
        return cast(Any, super()).callproc(procname, parameters)

    def copy_from(
        self,
        file: Any,
        table: Any,
        sep: str = "\t",
        null: str = "\\N",
        size: int = 8192,
        columns: Any = None,
    ) -> Any:
        return cast(Any, super()).copy_from(
            file, table, sep=sep, null=null, size=size, columns=columns
        )

    def copy_to(
        self,
        file: Any,
        table: Any,
        sep: str = "\t",
        null: str = "\\N",
        columns: Any = None,
    ) -> Any:
        return cast(Any, super()).copy_to(
            file, table, sep=sep, null=null, columns=columns
        )

    def copy_expert(self, sql: Any, file: Any, size: int = 8192) -> Any:
        return cast(Any, super()).copy_expert(sql, file, size=size)


class ConnectionMixin:
    """The recording overrides placed ahead of a psycopg2 connection
    class: cursors come back as recording subclasses, and the
    transaction boundaries call through to the base and are bound by
    wrapture below."""

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        # A cursor class named in the call (by keyword or in the
        # second positional slot) is substituted with its recording
        # subclass before the base builds the cursor.

        if "cursor_factory" in kwargs:
            factory = kwargs["cursor_factory"]
        elif len(args) > 1:
            factory = args[1]
        else:
            factory = None

        if factory is not None:
            recording = recording_cursor(factory)
            if "cursor_factory" in kwargs:
                kwargs["cursor_factory"] = recording
            else:
                args = (args[0], recording, *args[2:])

        # With none named and nothing set on the connection, a base
        # that falls straight through to the C method would build an
        # instance of the C cursor type, which cannot be reclassed
        # afterwards, so its recording subclass is named explicitly.

        elif getattr(self, "cursor_factory", None) is None and _falls_through(
            type(self)
        ):
            kwargs["cursor_factory"] = recording_cursor(_c_cursor_type())

        cursor = cast(Any, super()).cursor(*args, **kwargs)

        # A base's own cursor() may have supplied its default cursor
        # class instead; its instance is reclassed on the way out.

        if not isinstance(cursor, CursorMixin):
            try:
                cursor.__class__ = recording_cursor(type(cursor))
            except TypeError:
                pass

        return cursor

    def commit(self) -> Any:
        return cast(Any, super()).commit()

    def rollback(self) -> Any:
        return cast(Any, super()).rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any:
        return cast(Any, super()).__exit__(exc_type, exc_value, traceback)


def _c_cursor_type() -> type:
    import psycopg2.extensions

    return cast(type, psycopg2.extensions.cursor)


def _falls_through(cls: type) -> bool:
    """Whether cursor() on the given recording connection class, past
    the mixin, is psycopg2's own C method rather than an override of
    a base class supplying a cursor default of its own."""

    import psycopg2.extensions

    for base in cls.__mro__:
        if base is ConnectionMixin:
            continue
        if "cursor" in vars(base):
            return base is psycopg2.extensions.connection

    return True


def _recording(cache: dict[type, type], mixin: type, base: type) -> type:
    if isinstance(base, type) and issubclass(base, mixin):
        return base

    try:
        return cache[base]
    except KeyError:
        pass

    made = type(
        base.__name__,
        (mixin, base),
        {"__module__": base.__module__, "__qualname__": base.__qualname__},
    )
    cache[base] = made

    return made


def recording_cursor(base: type) -> type:
    """The recording subclass of a psycopg2 cursor class, made once
    per class; a class already recording is returned as is."""

    return _recording(_cursors, CursorMixin, base)


def recording_connection(base: type) -> type:
    """The recording subclass of a psycopg2 connection class, made
    once per class; a class already recording is returned as is."""

    return _recording(_connections, ConnectionMixin, base)


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind the connect factory and the mixins' methods; register
    their removal as this trigger's cleanup."""

    settings = instrumentation.settings
    record_statement = bool(settings["statement"])

    def data_for(
        cursor: Any, query: Any, operation: str | None = None
    ) -> dict[str, Any]:
        return query_data(
            query, cursor, cursor.connection.info, record_statement, operation
        )

    def opens(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, operation="CONNECT")

        # The requested connection class (keyword, or the second
        # positional slot) is replaced with its recording subclass.

        if "connection_factory" in kwargs:
            factory = kwargs["connection_factory"]
        elif len(args) > 1:
            factory = args[1]
        else:
            factory = None

        recording = recording_connection(factory or module.extensions.connection)
        if "connection_factory" in kwargs or len(args) <= 1:
            kwargs["connection_factory"] = recording
        else:
            args = (args[0], recording, *args[2:])

        connection = wrapped(*args, **kwargs)
        wrapture.annotate(**server_of(connection.info))

        return connection

    def queries(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        query = args[0] if args else kwargs.get("query")
        wrapture.annotate(**data_for(instance, query))

        return wrapped(*args, **kwargs)

    def calls(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        procname = args[0] if args else kwargs.get("procname")
        data = data_for(instance, None, "CALL")
        if isinstance(procname, str):
            data["procedure"] = procname
        wrapture.annotate(**data)

        return wrapped(*args, **kwargs)

    def copies_table(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        table = args[1] if len(args) > 1 else kwargs.get("table")
        data = data_for(instance, None, "COPY")
        if isinstance(table, str):
            data["collection"] = table
        wrapture.annotate(**data)

        return wrapped(*args, **kwargs)

    def copies_statement(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        sql = args[0] if args else kwargs.get("sql")
        wrapture.annotate(**data_for(instance, sql, "COPY"))

        return wrapped(*args, **kwargs)

    def performs(operation: str) -> Any:
        def record(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            wrapture.annotate(
                system=SYSTEM, operation=operation, **server_of(instance.info)
            )

            return wrapped(*args, **kwargs)

        return record

    def leaves(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        # The exit commits unless an exception is on its way through.

        exc_type = args[0] if args else kwargs.get("exc_type")
        operation = "COMMIT" if exc_type is None else "ROLLBACK"
        wrapture.annotate(
            system=SYSTEM, operation=operation, **server_of(instance.info)
        )

        return wrapped(*args, **kwargs)

    def database_binding(
        owner: Any, name: str, label: str | None = None, capture_args: Any = captured
    ) -> wrapture.Binding:
        return wrapture.binding(
            owner,
            name,
            label=label,
            category="database",
            leaf=True,
            capture_args=capture_args,
            capture_result=captured,
        )

    named: dict[str, wrapture.Binding] = {}

    # The factory: the module attribute's path says psycopg2:connect
    # already, so it takes no label, and none of its arguments are
    # captured (the dsn carries the password).

    connect = database_binding(module, "connect", capture_args="none")
    connect.on_call.decorates(opens)
    named["connect"] = connect

    # The cursor methods, labelled with the psycopg2 names they stand
    # for.

    for method, decorator in (
        ("execute", queries),
        ("executemany", queries),
        ("callproc", calls),
        ("copy_from", copies_table),
        ("copy_to", copies_table),
        ("copy_expert", copies_statement),
    ):
        bound = database_binding(
            CursorMixin, method, label=f"psycopg2.extensions:cursor.{method}"
        )
        bound.on_call.decorates(decorator)
        named[f"cursor_{method}"] = bound

    # The transaction boundaries: the explicit calls, and the context
    # manager exit that performs one of them.

    for method, operation in (("commit", "COMMIT"), ("rollback", "ROLLBACK")):
        bound = database_binding(
            ConnectionMixin, method, label=f"psycopg2.extensions:connection.{method}"
        )
        bound.on_call.decorates(performs(operation))
        named[f"connection_{method}"] = bound

    closes = database_binding(
        ConnectionMixin, "__exit__", label="psycopg2.extensions:connection.__exit__"
    )
    closes.on_call.decorates(leaves)
    named["connection_exit"] = closes

    group = wrapture.bindings(**named)
    group.apply()

    instrumentation.on_cleanup(group.remove)
