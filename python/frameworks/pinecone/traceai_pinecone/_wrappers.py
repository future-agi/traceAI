"""Shared operation attributes and sync/awaited Pinecone span lifecycles."""

import inspect
import logging
from contextlib import contextmanager
from typing import Any, Awaitable, Callable

from opentelemetry import context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

from traceai_pinecone._attributes import (
    VectorDBAttributes as Attrs,
    get_common_attributes,
    safe_json_dumps,
)

logger = logging.getLogger(__name__)
_ACTIVE = context.create_key("traceai-pinecone-active")


class BaseWrapper:
    """Keep instrumentation failures separate from SDK failures."""

    operation = ""

    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _parameters(self, wrapped, args, kwargs):
        if not args:
            return kwargs
        try:
            # Legacy gRPC accepts positional parameters, including async_req.
            bound = inspect.signature(wrapped).bind_partial(*args, **kwargs)
            params = dict(bound.arguments)
            params.update(params.pop("kwargs", {}))
            return params
        except (TypeError, ValueError):
            return kwargs

    def _suppressed(self, params):
        return (
            context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)
            or context.get_value(_ACTIVE)
            # Do not claim success for merely scheduling a legacy future.
            # These SDK-specific future APIs are deliberately outside coverage.
            or params.get("async_req")
            or params.get("async_threadpool_executor")
        )

    def _attributes(self, instance, params):
        # Read stored SDK state without invoking Index's plugin-loading
        # __getattr__ for missing names. Never infer an index name from its host.
        state = vars(instance)
        config = state.get("_config") or state.get("config")
        name = (
            getattr(config, "index_name", None)
            or state.get("name")
            or state.get("_index_name")
        )
        attributes = get_common_attributes(
            "pinecone", self.operation, name if isinstance(name, str) else None
        )
        host = state.get("_host") or getattr(config, "host", None)
        if isinstance(host, str) and host:
            attributes[Attrs.INDEX_HOST] = host
        if self.operation != "describe_index_stats":
            attributes[Attrs.NAMESPACE] = params.get("namespace") or ""
        if params.get("filter"):
            attributes[Attrs.QUERY_FILTER] = safe_json_dumps(params["filter"])
        attributes.update(self._request_attributes(params))
        return attributes

    def _request_attributes(self, params):
        return {}

    def _response_attributes(self, result):
        return {}

    @contextmanager
    def _span(self, instance, params):
        attributes = get_common_attributes("pinecone", self.operation)
        try:
            attributes.update(self._attributes(instance, params))
        except Exception as error:
            # Diagnostics must not echo SDK payloads in exception messages.
            logger.debug("Could not extract Pinecone request attributes (%s)", type(error).__name__)
        with self._tracer.start_as_current_span(
            f"pinecone {self.operation}", kind=SpanKind.CLIENT,
            attributes=attributes, record_exception=False, set_status_on_exception=False,
        ) as span:
            token = context.attach(context.set_value(_ACTIVE, True))
            try:
                yield span
            except BaseException as error:
                # CancelledError is a BaseException. End the awaited operation,
                # record one event, and propagate the exact exception unchanged.
                span.set_status(Status(StatusCode.ERROR, str(error)))
                span.record_exception(error)
                raise
            finally:
                context.detach(token)

    def _finish(self, span, result):
        try:
            span.set_attributes(self._response_attributes(result))
        except Exception as error:
            logger.debug("Could not extract Pinecone response attributes (%s)", type(error).__name__)
        span.set_status(Status(StatusCode.OK))

    def __call__(self, wrapped: Callable[..., Any], instance: Any, args: tuple, kwargs: dict) -> Any:
        params = self._parameters(wrapped, args, kwargs)
        if self._suppressed(params):
            return wrapped(*args, **kwargs)
        with self._span(instance, params) as span:
            result = wrapped(*args, **kwargs)
            self._finish(span, result)
            return result


class AsyncWrapper:
    """Apply the same operation contract after awaiting, not coroutine creation."""

    def __init__(self, operation: BaseWrapper):
        self._operation = operation

    async def __call__(
        self, wrapped: Callable[..., Awaitable[Any]], instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        operation = self._operation
        params = operation._parameters(wrapped, args, kwargs)
        if operation._suppressed(params):
            return await wrapped(*args, **kwargs)
        with operation._span(instance, params) as span:
            result = await wrapped(*args, **kwargs)
            operation._finish(span, result)
            return result


class QueryWrapper(BaseWrapper):
    operation = "query"

    def _request_attributes(self, params):
        return {
            Attrs.QUERY_TOP_K: params.get("top_k", 10),
            Attrs.QUERY_INCLUDE_METADATA: bool(params.get("include_metadata", False)),
            Attrs.QUERY_INCLUDE_VECTORS: bool(params.get("include_values", False)),
        }

    def _response_attributes(self, result):
        matches = getattr(result, "matches", None)
        if matches is None:
            return {}
        attributes = {Attrs.RESULTS_COUNT: len(matches)}
        scores = [m.score for m in matches[:10] if hasattr(m, "score")]
        ids = [m.id for m in matches[:10] if hasattr(m, "id")]
        if scores:
            attributes[Attrs.RESULTS_SCORES] = safe_json_dumps(scores)
        if ids:
            attributes[Attrs.RESULTS_IDS] = safe_json_dumps(ids)
        return attributes


class UpsertWrapper(BaseWrapper):
    operation = "upsert"

    def _request_attributes(self, params):
        vectors = params.get("vectors", [])
        attributes = {Attrs.UPSERT_COUNT: len(vectors)}
        if len(vectors):
            first = vectors[0]
            if isinstance(first, dict):
                values = first.get("values")
            elif isinstance(first, tuple) and len(first) >= 2:
                values = first[1]
            else:
                values = getattr(first, "values", None)
            if values is not None:
                attributes[Attrs.UPSERT_DIMENSIONS] = len(values)
        return attributes

    def _response_attributes(self, result):
        count = getattr(result, "upserted_count", None)
        return {"db.vector.upserted_count": count} if count is not None else {}


class DeleteWrapper(BaseWrapper):
    operation = "delete"

    def _request_attributes(self, params):
        attributes = {Attrs.DELETE_ALL: bool(params.get("delete_all", False))}
        ids = params.get("ids")
        if ids is not None:
            attributes[Attrs.DELETE_COUNT] = len(ids)
        return attributes


class FetchWrapper(BaseWrapper):
    operation = "fetch"

    def _request_attributes(self, params):
        ids = params.get("ids")
        return {"db.vector.fetch.ids_count": len(ids)} if ids is not None else {}

    def _response_attributes(self, result):
        vectors = getattr(result, "vectors", None)
        return {Attrs.RESULTS_COUNT: len(vectors)} if vectors is not None else {}


class UpdateWrapper(BaseWrapper):
    operation = "update"

    def _request_attributes(self, params):
        attributes = {"db.vector.update.id": str(params.get("id", ""))}
        if params.get("set_metadata"):
            attributes["db.vector.update.has_metadata"] = True
        values = params.get("values")
        if values is not None:
            attributes["db.vector.update.has_values"] = True
            attributes[Attrs.UPSERT_DIMENSIONS] = len(values)
        return attributes


class DescribeIndexStatsWrapper(BaseWrapper):
    operation = "describe_index_stats"

    def _response_attributes(self, result):
        attributes = {}
        for attr, field in (
            ("db.vector.stats.total_count", "total_vector_count"),
            (Attrs.INDEX_DIMENSIONS, "dimension"),
        ):
            value = getattr(result, field, None)
            if value is not None:
                attributes[attr] = value
        namespaces = getattr(result, "namespaces", None)
        if namespaces is not None:
            attributes["db.vector.stats.namespace_count"] = len(namespaces)
        return attributes
