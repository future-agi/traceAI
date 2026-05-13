"""LanceDB method wrappers for OpenTelemetry instrumentation."""

import json
import logging
from typing import Any, Callable

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

# FI canonical span-kind / IO keys. Optional dependency.
try:
    from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes

    _FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
    _FI_INPUT_VALUE = SpanAttributes.INPUT_VALUE
    _FI_INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
    _FI_OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
    _FI_OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
    _FI_RETRIEVER = FiSpanKindValues.RETRIEVER.value
except Exception:  # pragma: no cover
    _FI_SPAN_KIND = "gen_ai.span.kind"
    _FI_INPUT_VALUE = "input.value"
    _FI_INPUT_MIME_TYPE = "input.mime_type"
    _FI_OUTPUT_VALUE = "output.value"
    _FI_OUTPUT_MIME_TYPE = "output.mime_type"
    _FI_RETRIEVER = "RETRIEVER"

logger = logging.getLogger(__name__)


def safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return str(obj)


class BaseWrapper:
    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_table_name(self, instance: Any) -> str:
        try:
            if hasattr(instance, "name"):
                return instance.name
            if hasattr(instance, "_name"):
                return instance._name
        except Exception:
            pass
        return "unknown"

    def _get_attributes(self, operation: str, table_name: str) -> dict:
        return {
            "db.system": "lancedb",
            "db.operation.name": operation,
            "db.namespace": table_name,
            "db.vector.collection.name": table_name,
        }


class SearchWrapper(BaseWrapper):
    def __init__(self, tracer: Tracer, method: str = "to_list"):
        super().__init__(tracer)
        self._method = method

    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        table_name = self._get_table_name(instance)

        # Try to get limit from query builder
        limit = getattr(instance, "_limit", 10)

        attributes = self._get_attributes("search", table_name)
        attributes["db.vector.query.top_k"] = limit
        attributes["db.vector.search.output_format"] = self._method

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        # LanceDB uses a query-builder pattern; the actual query data is on
        # the builder instance. Surface what we can find safely.
        query_value = getattr(instance, "_query", None) or getattr(
            instance, "_text", None
        )
        input_summary: dict = {"limit": limit, "output_format": self._method}
        if isinstance(query_value, str):
            input_summary["query"] = query_value[:500]
        elif isinstance(query_value, (list, tuple)):
            input_summary["vector_dim"] = len(query_value)
        attributes[_FI_INPUT_VALUE] = safe_json_dumps(input_summary)
        attributes[_FI_INPUT_MIME_TYPE] = "application/json"

        with self._tracer.start_as_current_span("lancedb search", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                if result is not None:
                    if self._method == "to_list":
                        span.set_attribute("db.vector.results.count", len(result))
                        span.set_attribute(
                            _FI_OUTPUT_VALUE, safe_json_dumps(result[:50])
                        )
                        span.set_attribute(
                            _FI_OUTPUT_MIME_TYPE, "application/json"
                        )
                    elif hasattr(result, "num_rows"):
                        span.set_attribute("db.vector.results.count", result.num_rows)
                        # Best-effort: convert pyarrow table to a dict list.
                        try:
                            if hasattr(result, "to_pylist"):
                                rows = result.to_pylist()
                            elif hasattr(result, "to_pydict"):
                                rows = result.to_pydict()
                            else:
                                rows = None
                            if rows is not None:
                                span.set_attribute(
                                    _FI_OUTPUT_VALUE,
                                    safe_json_dumps(rows[:50] if isinstance(rows, list) else rows),
                                )
                                span.set_attribute(
                                    _FI_OUTPUT_MIME_TYPE, "application/json"
                                )
                        except Exception:
                            pass
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class AddWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        table_name = self._get_table_name(instance)
        data = args[0] if args else kwargs.get("data", [])

        attributes = self._get_attributes("add", table_name)
        if hasattr(data, "__len__"):
            attributes["db.vector.upsert.count"] = len(data)

        with self._tracer.start_as_current_span("lancedb add", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class UpdateWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        table_name = self._get_table_name(instance)
        where = kwargs.get("where")

        attributes = self._get_attributes("update", table_name)
        if where:
            attributes["db.vector.query.filter"] = str(where)

        with self._tracer.start_as_current_span("lancedb update", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DeleteWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        table_name = self._get_table_name(instance)
        where = args[0] if args else kwargs.get("where")

        attributes = self._get_attributes("delete", table_name)
        if where:
            attributes["db.vector.query.filter"] = str(where)

        with self._tracer.start_as_current_span("lancedb delete", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class CreateTableWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        table_name = args[0] if args else kwargs.get("name", "unknown")

        attributes = self._get_attributes("create_table", table_name)

        with self._tracer.start_as_current_span("lancedb create_table", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DropTableWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        table_name = args[0] if args else kwargs.get("name", "unknown")

        attributes = self._get_attributes("drop_table", table_name)

        with self._tracer.start_as_current_span("lancedb drop_table", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class OpenTableWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        table_name = args[0] if args else kwargs.get("name", "unknown")

        attributes = self._get_attributes("open_table", table_name)

        with self._tracer.start_as_current_span("lancedb open_table", kind=SpanKind.CLIENT, attributes=attributes) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
