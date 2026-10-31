"""Privacy-aware wrappers shared by synchronous and asynchronous clients."""

import inspect
import json
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.trace import SpanKind, Status, StatusCode
from wrapt import ObjectProxy

_ACTIVE_CALL: ContextVar[bool] = ContextVar("traceai_milvus_active_call", default=False)
_ITERATORS = {"search_iterator", "query_iterator"}
_READS = {"search", "hybrid_search", "query", "get"}


class OperationWrapper:
    def __init__(
        self, tracer: FITracer, config: TraceConfig, operation: str, original: Callable
    ):
        self._tracer = tracer
        self._config = config
        self._operation = operation
        self._signature = inspect.signature(original)
        self._async = inspect.iscoroutinefunction(inspect.unwrap(original))
        self.enabled = True

    def _skip(self) -> bool:
        return (
            not self.enabled
            or _ACTIVE_CALL.get()
            or bool(get_value(_SUPPRESS_INSTRUMENTATION_KEY))
        )

    def _attributes(self, instance: Any, args: tuple, kwargs: dict) -> dict:
        # Bind the installed SDK's signature rather than hard-coding argument
        # positions (e.g. delete's pks -> ids rename). Never mutate caller data.
        try:
            bound = self._signature.bind(instance, *args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
            for name, parameter in self._signature.parameters.items():
                if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                    params.update(params.pop(name, {}))
        except TypeError:
            # Let the SDK raise its original argument error inside the span.
            params = {}
        collection = params.get("collection_name", "unknown")
        if not isinstance(collection, str):
            collection = "unknown"
        attributes = {
            "db.system": "milvus",
            "db.operation.name": self._operation,
            "db.namespace": collection,
            "db.vector.collection.name": collection,
        }
        limit = params.get("limit")
        if (
            self._operation in _READS | _ITERATORS
            and isinstance(limit, int)
            and limit >= 0
        ):
            attributes["db.vector.query.top_k"] = limit
        batch_size = params.get("batch_size")
        if self._operation in _ITERATORS and isinstance(batch_size, int):
            attributes["db.vector.query.batch_size"] = batch_size
        # FITracer does not classify custom db.* attributes as inputs. Omit
        # these BEFORE start_span so samplers/processors cannot see hidden data.
        if not (self._config.hide_inputs or self._config.hide_input_text):
            query_filter = params.get("filter")
            if isinstance(query_filter, str) and query_filter:
                attributes["db.vector.query.filter"] = query_filter
            fields = params.get("output_fields")
            if isinstance(fields, (list, tuple)) and all(
                isinstance(field, str) for field in fields
            ):
                attributes["db.vector.query.output_fields"] = json.dumps(fields)
        if self._operation in {"insert", "upsert"}:
            data = params.get("data")
            if isinstance(data, (list, tuple, dict)):
                attributes["db.vector.upsert.count"] = (
                    1 if isinstance(data, dict) else len(data)
                )
        if self._operation in {"delete", "get"}:
            ids = params.get("ids", params.get("pks"))
            if isinstance(ids, (list, tuple, str, int)):
                key = (
                    "db.vector.delete.count"
                    if self._operation == "delete"
                    else "db.vector.query.ids_count"
                )
                attributes[key] = len(ids) if isinstance(ids, (list, tuple)) else 1
        dimension = params.get("dimension")
        if self._operation == "create_collection" and isinstance(dimension, int):
            attributes["db.vector.dimension"] = dimension
        # Vectors, inserted documents, IDs, hybrid requests and result content
        # are never serialized. Only counts and the controls above are captured.
        return attributes

    @contextmanager
    def _span(self, operation: str, attributes: dict):
        token = _ACTIVE_CALL.set(True)
        try:
            with self._tracer.start_as_current_span(
                f"milvus {operation}",
                kind=SpanKind.CLIENT,
                attributes={**attributes, "db.operation.name": operation},
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    yield span
                except StopIteration:
                    span.set_status(Status(StatusCode.OK))
                    raise
                except BaseException as error:
                    # Includes asyncio cancellation; preserve the original error
                    # and record exactly once, through FITracer's privacy policy.
                    description = (
                        None if self._config.hide_error_details else str(error)
                    )
                    span.set_status(Status(StatusCode.ERROR, description))
                    span.record_exception(error)
                    raise
                else:
                    span.set_status(Status(StatusCode.OK))
        finally:
            _ACTIVE_CALL.reset(token)

    def _result(self, span: Any, result: Any, attributes: dict) -> Any:
        if self._operation in _ITERATORS:
            return _IteratorProxy(result, self, attributes)
        if self._operation in _READS and isinstance(result, (list, tuple)):
            if self._operation in {"search", "hybrid_search"}:
                count = sum(
                    len(hits) for hits in result if isinstance(hits, (list, tuple))
                )
            else:
                count = len(result)
            span.set_attribute("db.vector.results.count", count)
        return result

    def __call__(
        self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        if self._async:
            return self._await_call(wrapped, instance, args, kwargs)
        if self._skip():
            return wrapped(*args, **kwargs)
        attributes = self._attributes(instance, args, kwargs)
        with self._span(self._operation, attributes) as span:
            return self._result(span, wrapped(*args, **kwargs), attributes)

    async def _await_call(
        self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        if self._skip():
            return await wrapped(*args, **kwargs)
        attributes = self._attributes(instance, args, kwargs)
        with self._span(self._operation, attributes) as span:
            return self._result(span, await wrapped(*args, **kwargs), attributes)


class _IteratorProxy(ObjectProxy):
    """Trace SDK .next()/.close() calls, not idle time between pages.

    Construction has its own span because it can perform RPCs. Each fetch is a
    bounded span under the caller's current context: abandoning an iterator
    cannot leave an open span or context, and no pages are eagerly consumed.
    """

    def __init__(self, wrapped: Any, wrapper: OperationWrapper, attributes: dict):
        super().__init__(wrapped)
        self._self_wrapper = wrapper
        self._self_attributes = attributes

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        wrapper = self._self_wrapper
        call = getattr(self.__wrapped__, method)
        if wrapper._skip():
            return call(*args, **kwargs)
        with wrapper._span(
            f"{wrapper._operation}.{method}", self._self_attributes
        ) as span:
            result = call(*args, **kwargs)
            if method == "next":
                # Search pages can be SDK Hits/SearchPage objects, not lists.
                # len() must not consume the iterator or disrupt a valid result.
                try:
                    count = len(result)
                except Exception:  # noqa: BLE001 - telemetry must not change SDK results
                    return result
                else:
                    span.set_attribute("db.vector.results.count", count)
            return result

    def next(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("next", *args, **kwargs)

    def close(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("close", *args, **kwargs)
