"""The `transaction()` block seams: entering and leaving a
Transaction or AsyncTransaction, recorded as database events.

`with conn.transaction():` is psycopg's transaction block. Entering
issues BEGIN when no transaction is open on the connection, and a
SAVEPOINT for a nested block (or when a savepoint name was asked
for); leaving issues COMMIT or RELEASE SAVEPOINT, or ROLLBACK (to the
savepoint, for a nested block) when an exception is passing through,
`force_rollback` was set, or `psycopg.Rollback` was raised inside.
The block object knows which it did: whether it opened the outermost
transaction, and its savepoint name, both read after the enter has
run and before the exit does. These statements go to libpq directly,
never through a cursor, so the enter and exit are bound in their own
right, each recording the operation it performed and the savepoint
name when one was involved.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import SYSTEM, captured, server_of


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind the enter and exit of the sync and async transaction
    blocks; register their removal as this trigger's cleanup."""

    def entered(instance: Any) -> dict[str, Any]:
        # Known only once the enter has run: whether this block began
        # the transaction or nested inside one.

        outer = bool(getattr(instance, "_outer_transaction", False))
        savepoint = getattr(instance, "_savepoint_name", "") or None

        data: dict[str, Any] = {"operation": "BEGIN" if outer else "SAVEPOINT"}
        if savepoint:
            data["savepoint"] = savepoint

        return data

    def leaving(
        instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        exc_value = args[1] if len(args) > 1 else kwargs.get("exc_val")
        outer = bool(getattr(instance, "_outer_transaction", False))
        savepoint = getattr(instance, "_savepoint_name", "") or None
        commits = exc_value is None and not getattr(instance, "force_rollback", False)

        if commits:
            operation = "COMMIT" if outer else "RELEASE"
        else:
            operation = "ROLLBACK"

        data: dict[str, Any] = {"operation": operation}
        if savepoint:
            data["savepoint"] = savepoint

        return data

    def enters(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, **server_of(instance.connection.info))

        outcome = wrapped(*args, **kwargs)
        wrapture.annotate(**entered(instance))

        return outcome

    async def enters_async(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, **server_of(instance.connection.info))

        outcome = await wrapped(*args, **kwargs)
        wrapture.annotate(**entered(instance))

        return outcome

    def exits(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(
            system=SYSTEM,
            **server_of(instance.connection.info),
            **leaving(instance, args, kwargs),
        )

        return wrapped(*args, **kwargs)

    async def exits_async(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(
            system=SYSTEM,
            **server_of(instance.connection.info),
            **leaving(instance, args, kwargs),
        )

        return await wrapped(*args, **kwargs)

    def boundary(owner: Any, name: str, decorator: Any) -> wrapture.Binding:
        binding = wrapture.binding(
            owner,
            name,
            category="database",
            leaf=True,
            capture_args=captured,
            capture_result=captured,
        )
        binding.on_call.decorates(decorator)

        return binding

    group = wrapture.bindings(
        enter=boundary(module.Transaction, "__enter__", enters),
        exit=boundary(module.Transaction, "__exit__", exits),
        async_enter=boundary(module.AsyncTransaction, "__aenter__", enters_async),
        async_exit=boundary(module.AsyncTransaction, "__aexit__", exits_async),
    )
    group.apply()

    instrumentation.on_cleanup(group.remove)
