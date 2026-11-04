"""Trace public executions, not the nested materialization steps they invoke."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from weakref import WeakKeyDictionary

from opentelemetry import context
from opentelemetry.instrumentation.utils import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, StatusCode, Tracer, use_span

from traceai_lancedb._readers import AsyncReader, SyncReader

logger = logging.getLogger(__name__)

_ACTIVE: ContextVar[Execution | None] = ContextVar("lancedb_execution", default=None)
# Hybrid queries create their constituent builders before dispatching them to
# worker threads (which do not copy ContextVars). Associate only those builders
# with their execution, rather than suppressing unrelated queries globally.
_PARENTS: WeakKeyDictionary = WeakKeyDictionary()


def remember_parent(wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
    result = wrapped(*args, **kwargs)
    execution = _ACTIVE.get()
    if execution is not None and not execution.finished:
        _PARENTS[instance] = execution
    return result


def _parent(instance: Any) -> Execution | None:
    execution = _ACTIVE.get() or _PARENTS.get(instance)
    return execution if execution is not None and not execution.finished else None


def _row_count(value: Any) -> int | None:
    # Never iterate a generator or consume a reader to collect telemetry.
    try:
        count = getattr(value, "num_rows", None)
        if isinstance(count, int):
            return count
        if isinstance(value, (list, tuple)) or type(value).__module__.split(".")[0] in (
            "pandas",
            "polars",
        ):
            return len(value)
    except Exception as error:  # noqa: BLE001 - metadata is best-effort
        # Only the type is safe to log: property errors may include user data.
        logger.debug("LanceDB metadata unavailable (%s)", type(error).__name__)
    return None


def _attributes(
    operation: str, method: str, instance: Any, args: tuple, kwargs: dict
) -> dict:
    attributes = {"db.system": "lancedb", "db.operation.name": operation}
    # Metadata must not execute a query or prevent the application operation.
    try:
        if operation in ("create_table", "open_table", "drop_table"):
            name = args[0] if args else kwargs.get("name")
        else:
            table = (
                getattr(instance, "_table", None) if operation == "search" else instance
            )
            name = getattr(table, "name", None) or getattr(table, "_name", None)
        if isinstance(name, str):
            attributes["db.namespace"] = name
            attributes["db.vector.collection.name"] = name
        if operation == "search":
            attributes["db.vector.search.output_format"] = method
            limit = getattr(instance, "_limit", None)
            if isinstance(limit, int) and limit >= 0:
                attributes["db.vector.query.top_k"] = limit
        elif operation == "add":
            count = _row_count(args[0] if args else kwargs.get("data"))
            if count is not None:
                attributes["db.vector.upsert.count"] = count
    except Exception as error:  # noqa: BLE001 - metadata is best-effort
        # Only the type is safe to log: property errors may include user data.
        logger.debug("LanceDB metadata unavailable (%s)", type(error).__name__)
    return attributes


class Execution:
    def __init__(self, tracer: Tracer, operation: str, attributes: dict) -> None:
        self.span = tracer.start_span(
            "lancedb " + operation, kind=SpanKind.CLIENT, attributes=attributes
        )
        self.finished = False
        self.rows = 0

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _ACTIVE.set(self)
        try:
            # Do not let OTel's context manager serialize exception messages,
            # stack traces or locals (database errors can contain row contents).
            with use_span(
                self.span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            ):
                yield
        finally:
            _ACTIVE.reset(token)

    def count(self, rows: int) -> None:
        if not self.finished:
            self.rows += rows
            self.span.set_attribute("db.vector.results.count", self.rows)

    def finish(
        self, *, complete: bool = False, error: BaseException | None = None
    ) -> None:
        if self.finished:
            return
        self.finished = True
        if error is not None:
            error_type = type(error).__module__ + "." + type(error).__qualname__
            self.span.set_attribute("error.type", error_type)
            self.span.set_status(StatusCode.ERROR)
            self.span.add_event("exception", {"exception.type": error_type})
        elif complete:
            self.span.set_status(StatusCode.OK)
        self.span.end()


class OperationWrapper:
    def __init__(self, tracer: Tracer, operation: str, method: str) -> None:
        self.tracer = tracer
        self.operation = operation
        self.method = method

    def _execution(self, instance: Any, args: tuple, kwargs: dict) -> Execution:
        return Execution(
            self.tracer,
            self.operation,
            _attributes(self.operation, self.method, instance, args, kwargs),
        )

    def _result(
        self, result: Any, execution: Execution, *, asynchronous: bool = False
    ) -> Any:
        if self.method == "to_batches":
            # Ownership transfers to the reader until exhaustion, explicit close,
            # cancellation/failure, or garbage collection. No context is left
            # attached between batches and no data is read ahead.
            execution.count(0)
            return (
                AsyncReader(result, execution)
                if asynchronous
                else SyncReader(result, execution)
            )
        if self.operation == "search":
            count = _row_count(result)
            if count is not None:
                execution.count(count)
        execution.finish(complete=True)
        return result

    def __call__(
        self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        parent = _parent(instance)
        if parent is not None:
            with parent.activate():
                return wrapped(*args, **kwargs)
        execution = self._execution(instance, args, kwargs)
        try:
            with execution.activate():
                result = wrapped(*args, **kwargs)
            return self._result(result, execution)
        except BaseException as error:
            execution.finish(error=error)
            raise

    async def asynchronous(
        self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        parent = _parent(instance)
        if parent is not None:
            with parent.activate():
                return await wrapped(*args, **kwargs)
        execution = self._execution(instance, args, kwargs)
        try:
            with execution.activate():
                result = await wrapped(*args, **kwargs)
            return self._result(result, execution, asynchronous=True)
        except BaseException as error:
            execution.finish(error=error)
            raise
