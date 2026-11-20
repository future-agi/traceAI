"""Qdrant method wrappers for OpenTelemetry instrumentation."""

import inspect
import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)

# Suppress only direct SDK text-query delegation, not independent operations in
# embedding callbacks or other clients/tasks that inherit the current context.
_TEXT_QUERY: ContextVar[Any] = ContextVar("qdrant_text_query", default=None)
_TEXT_DELEGATES = {
    "query": ("search", "query_points", "search_batch", "query_batch_points"),
    "query_batch": ("search_batch", "query_batch_points"),
}


def safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return str(obj)


def _arguments(wrapped: Callable, args: tuple, kwargs: dict) -> dict:
    """Use the installed SDK signature, including positional and default values."""
    bound = inspect.signature(wrapped).bind(*args, **kwargs)
    bound.apply_defaults()
    values = dict(bound.arguments)
    values.update(values.pop("kwargs", {}))
    return values


class BaseWrapper:
    _operation = "query"

    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_attributes(self, operation: str, collection_name: str) -> dict:
        return {
            "db.system": "qdrant",
            "db.operation.name": operation,
            "db.namespace": collection_name,
            "db.vector.collection.name": collection_name,
        }

    def _request_attributes(self, values: dict) -> dict:
        return {}

    def _record_result(self, span: Any, result: Any) -> None:
        pass

    def _should_skip(self, instance: Any, caller: Any) -> bool:
        return bool(context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY))

    @contextmanager
    def _span(self, wrapped: Callable, args: tuple, kwargs: dict):
        attributes = self._get_attributes(
            self._operation, args[0] if args else kwargs.get("collection_name", "unknown")
        )
        try:
            attributes.update(self._request_attributes(_arguments(wrapped, args, kwargs)))
        except Exception:
            # Telemetry extraction must not reject an SDK-supported input, or
            # replace the SDK's own argument-validation error.
            logger.debug("Could not extract Qdrant request metadata", exc_info=True)
        with self._tracer.start_as_current_span(
            f"qdrant {self._operation}", kind=SpanKind.CLIENT, attributes=attributes
        ) as span:
            yield span

    def _finish(self, span: Any, result: Any) -> None:
        if span.is_recording():
            try:
                self._record_result(span, result)
            except Exception:
                logger.debug("Could not extract Qdrant result metadata", exc_info=True)
        span.set_status(Status(StatusCode.OK))

    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        if self._should_skip(instance, sys._getframe(1)):
            return wrapped(*args, **kwargs)
        with self._span(wrapped, args, kwargs) as span:
            result = wrapped(*args, **kwargs)
            self._finish(span, result)
            return result

    async def async_call(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        if self._should_skip(instance, sys._getframe(1)):
            return await wrapped(*args, **kwargs)
        # Keep the span and context alive until the coroutine completes. The
        # context manager closes it on errors and cancellation without changing
        # the exception or recording it twice.
        with self._span(wrapped, args, kwargs) as span:
            result = await wrapped(*args, **kwargs)
            self._finish(span, result)
            return result


class QueryPointsWrapper(BaseWrapper):
    """Single, batch and grouped queries, including legacy text/search APIs."""

    def __init__(self, tracer: Tracer, method_name: str = "query_points"):
        super().__init__(tracer)
        self._method_name = method_name

    def _should_skip(self, instance: Any, caller: Any) -> bool:
        if super()._should_skip(instance, caller):
            return True
        parent = _TEXT_QUERY.get()
        if parent is None or parent[0] is not instance or self._method_name not in parent[2]:
            return False
        # C wrapt calls us directly; pure-Python wrapt inserts dispatch frames.
        # Do not skip user callbacks between the SDK query and this operation.
        while caller is not None and caller.f_globals.get("__name__", "").startswith("wrapt."):
            caller = caller.f_back
        return caller is not None and caller.f_code is parent[1]

    @contextmanager
    def _span(self, wrapped: Callable, args: tuple, kwargs: dict):
        token = None
        if self._method_name in _TEXT_DELEGATES:
            token = _TEXT_QUERY.set((
                getattr(wrapped, "__self__", None),
                getattr(inspect.unwrap(wrapped), "__code__", None),
                _TEXT_DELEGATES[self._method_name],
            ))
        try:
            with super()._span(wrapped, args, kwargs) as span:
                yield span
        finally:
            if token is not None:
                _TEXT_QUERY.reset(token)

    def _request_attributes(self, values: dict) -> dict:
        attributes = {}
        if "batch" in self._method_name:
            requests = values.get("requests", values.get("query_texts", []))
            attributes["db.vector.query.batch_size"] = len(requests)
        # Batch requests can have different limits/selectors: do not report
        # made-up defaults for the whole batch.
        limit_attribute = "group_limit" if "groups" in self._method_name else "top_k"
        for param, attr in (("limit", limit_attribute), ("group_size", "group_size"),
                            ("score_threshold", "score_threshold")):
            if values.get(param) is not None:
                attributes[f"db.vector.query.{attr}"] = values[param]
        for param in ("with_payload", "with_vectors"):
            # Only literal flags have boolean semantics. Selectors may contain
            # private field names or protobuf enable=False; never stringify them.
            if isinstance(values.get(param), bool):
                attributes[f"db.vector.query.{param}"] = values[param]
        if values.get("query_filter") is not None:
            attributes["db.vector.query.has_filter"] = True
        return attributes

    def _record_result(self, span: Any, result: Any) -> None:
        if result is None:
            return
        if "groups" in self._method_name:
            span.set_attribute("db.vector.results.groups_count", len(result.groups))
            batches = (group.hits for group in result.groups)
        elif "batch" in self._method_name:
            batches = (getattr(response, "points", response) for response in result)
        else:
            batches = (getattr(result, "points", result),)
        count = 0
        scores = []
        for points in batches:
            count += len(points)
            scores.extend(p.score for p in points[:max(0, 10 - len(scores))] if hasattr(p, "score"))
        span.set_attribute("db.vector.results.count", count)
        if scores:
            span.set_attribute("db.vector.results.scores", safe_json_dumps(scores))


# Keep SearchWrapper as alias for backwards compatibility.
SearchWrapper = QueryPointsWrapper


class UpsertWrapper(BaseWrapper):
    _operation = "upsert"

    def _request_attributes(self, values: dict) -> dict:
        points = values.get("points", [])
        # Qdrant accepts both a sequence of PointStruct and a columnar Batch.
        return {"db.vector.upsert.count": len(getattr(points, "ids", points))}


class DeleteWrapper(BaseWrapper):
    _operation = "delete"

    def _request_attributes(self, values: dict) -> dict:
        return {"db.vector.delete.has_selector": values.get("points_selector") is not None}


class RetrieveWrapper(BaseWrapper):
    _operation = "retrieve"

    def _request_attributes(self, values: dict) -> dict:
        return {"db.vector.retrieve.ids_count": len(values.get("ids", []))}

    def _record_result(self, span: Any, result: Any) -> None:
        if result is not None:
            span.set_attribute("db.vector.results.count", len(result))


class ScrollWrapper(BaseWrapper):
    _operation = "scroll"

    def _request_attributes(self, values: dict) -> dict:
        return {"db.vector.scroll.limit": values.get("limit", 10)}

    def _record_result(self, span: Any, result: Any) -> None:
        if result is not None:
            span.set_attribute("db.vector.results.count", len(result[0]))


class RecommendWrapper(QueryPointsWrapper):
    _operation = "recommend"

    def __init__(self, tracer: Tracer, method_name: str = "recommend"):
        super().__init__(tracer, method_name)

    def _request_attributes(self, values: dict) -> dict:
        attributes = super()._request_attributes(values)
        if "batch" not in self._method_name:
            attributes.update({
                "db.vector.recommend.positive_count": len(values.get("positive") or []),
                "db.vector.recommend.negative_count": len(values.get("negative") or []),
            })
        return attributes


class CountWrapper(BaseWrapper):
    _operation = "count"

    def _record_result(self, span: Any, result: Any) -> None:
        if result is not None:
            span.set_attribute("db.vector.collection.count", result.count)
