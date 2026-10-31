"""Redis vector spans without changing application results or consuming inputs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.trace import Span, SpanKind, StatusCode, Tracer

logger = logging.getLogger(__name__)
_VECTOR_SPAN_KEY = context_api.create_key("traceai_redis_vector_span")


def _collection_size(value: Any) -> int | None:
    # RedisVL also returns list subclasses. Bypass overridden __len__ methods.
    if isinstance(value, list):
        return list.__len__(value)
    if isinstance(value, tuple):
        return tuple.__len__(value)
    return None


class BaseWrapper:
    operation = ""
    span_name = ""
    suppress_commands = False

    def __init__(self, tracer: Tracer, *, is_async: bool = False) -> None:
        self._tracer = tracer
        self._is_async = is_async
        self.enabled = True

    def _attributes(self, instance: Any, args: tuple, kwargs: dict) -> dict:
        attributes = {"db.system": "redis", "db.operation.name": self.operation}
        # Metadata is best effort; a descriptor must not break the database call.
        try:
            for name in ("name", "index_name", "_name"):
                index_name = getattr(instance, name, None)
                if isinstance(index_name, str):
                    attributes["db.namespace"] = index_name
                    attributes["db.vector.collection.name"] = index_name
                    break
            if self.operation == "load":
                data = args[0] if args else kwargs.get("data")
                # Never consume generators or call user-defined __len__ methods.
                count = _collection_size(data)
                if count is not None:
                    attributes["db.vector.upsert.count"] = count
            elif self.operation in ("search", "query", "ft_search"):
                query = args[0] if args else kwargs.get("query")
                # RedisVL stores these on VectorQuery in both old and new APIs.
                top_k = getattr(query, "_num_results", None)
                if type(top_k) is int and top_k >= 0:
                    attributes["db.vector.query.top_k"] = top_k
                field = getattr(query, "_vector_field_name", None)
                if field is None:
                    field = getattr(query, "_field", None)
                if isinstance(field, str):
                    attributes["db.vector.query.field"] = field
                # Do not stringify queries (which can contain filters or vectors).
                from redisvl.query import VectorQuery

                if isinstance(query, VectorQuery):
                    attributes["db.vector.query.type"] = "knn"
        except Exception:  # noqa: BLE001 - telemetry accessors must not break the call
            # Do not log the exception/traceback: accessors may include query data.
            logger.debug("Redis span metadata extraction failed")
        return attributes

    def _finish(self, span: Span | None, result: Any) -> None:
        if span is None:
            return
        try:
            span.set_status(StatusCode.OK)
            if self.operation in ("search", "query", "ft_search"):
                # redis-py Result has docs/total, but no __len__. query() returns
                # a list. Unknown result shapes are passed through untouched.
                docs = (
                    result
                    if isinstance(result, (list, tuple))
                    else getattr(result, "docs", None)
                )
                count = _collection_size(docs)
                if count is not None:
                    span.set_attribute("db.vector.results.count", count)
                total = getattr(result, "total", None)
                if type(total) is int and total >= 0:
                    span.set_attribute("db.vector.results.total", total)
            elif self.operation == "load":
                count = _collection_size(result)
                if count is not None:
                    span.set_attribute("db.vector.upsert.count", count)
        except Exception:  # noqa: BLE001 - telemetry accessors must not break the call
            # Fail open without logging a possibly sensitive result/exception.
            logger.debug("Redis result metadata extraction failed")

    @contextmanager
    def _span(self, instance: Any, args: tuple, kwargs: dict) -> Iterator[Span | None]:
        if not self.enabled or context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            yield None
            return
        # A RedisVL query calls search, which calls redis-py FT.SEARCH. Only the
        # outer vector operation owns a span. Suppress maintained command tracing
        # at the FT boundary, not around RedisVL/user preprocessing work.
        commands = (
            suppress_instrumentation() if self.suppress_commands else nullcontext()
        )
        if context_api.get_value(_VECTOR_SPAN_KEY):
            with commands:
                yield None
            return
        with self._tracer.start_as_current_span(
            self.span_name,
            kind=SpanKind.CLIENT,
            attributes=self._attributes(instance, args, kwargs),
        ) as span:
            token = context_api.attach(context_api.set_value(_VECTOR_SPAN_KEY, True))
            try:
                with commands:
                    yield span
            finally:
                context_api.detach(token)

    def __call__(
        self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        if self._is_async:
            return self._async_call(wrapped, instance, args, kwargs)
        with self._span(instance, args, kwargs) as span:
            result = wrapped(*args, **kwargs)
            self._finish(span, result)
            return result

    async def _async_call(
        self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        # Some async APIs are regular functions returning an awaitable. Select
        # this path by client class, not inspect.iscoroutinefunction().
        with self._span(instance, args, kwargs) as span:
            try:
                result = await wrapped(*args, **kwargs)
            except asyncio.CancelledError as error:
                # OTel handles Exception, not cancellation (a BaseException).
                if span is not None:
                    span.record_exception(error, escaped=True)
                    span.set_status(StatusCode.ERROR)
                raise
            self._finish(span, result)
            return result


class SearchIndexSearchWrapper(BaseWrapper):
    operation = "search"
    span_name = "redis vector_search"


class SearchIndexLoadWrapper(BaseWrapper):
    operation = "load"
    span_name = "redis load"


class SearchIndexQueryWrapper(BaseWrapper):
    operation = "query"
    span_name = "redis query"


class FTSearchWrapper(BaseWrapper):
    operation = "ft_search"
    span_name = "redis ft_search"
    suppress_commands = True


class FTCreateWrapper(BaseWrapper):
    operation = "ft_create"
    span_name = "redis ft_create"
    suppress_commands = True


class FTDropIndexWrapper(BaseWrapper):
    operation = "ft_dropindex"
    span_name = "redis ft_dropindex"
    suppress_commands = True
