"""Shared sync/async tracing with metadata-only defaults."""

import json
import logging
from contextlib import contextmanager
from typing import Any, Awaitable, Callable

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.trace import INVALID_SPAN, SpanKind, Status, StatusCode, use_span

_QUERIES = {"near_vector", "near_text", "hybrid", "bm25", "fetch_objects"}
_TEXT_QUERIES = {"near_text", "hybrid", "bm25"}
_MAX_DOCUMENTS = 20
_MAX_CONTENT_LENGTH = 16_384
logger = logging.getLogger(__name__)


def _log_telemetry_failure(stage: str) -> None:
    # Error messages and traceback frames can themselves contain private data.
    logger.debug(
        "Weaviate %s failed; telemetry error details omitted for privacy", stage
    )


class OperationWrapper:
    def __init__(
        self,
        tracer: FITracer,
        operation: str,
        *,
        config: TraceConfig,
        capture_content: bool = False,
    ) -> None:
        self._tracer = tracer
        self._operation = operation
        self._config = config
        self._capture_content = capture_content

    def _attributes(self, instance, args, kwargs):
        operation = self._operation
        attributes = {
            "db.system": "weaviate",
            "db.operation.name": operation,
            SpanAttributes.GEN_AI_SPAN_KIND: (
                "RETRIEVER" if operation in _QUERIES else "VECTOR_DB"
            ),
        }
        try:
            name = getattr(instance, "_name", None) or getattr(instance, "name", None)
            if isinstance(name, str):
                attributes["db.namespace"] = name
                attributes["db.vector.collection.name"] = name
            if operation in _QUERIES:
                attributes["db.vector.query.type"] = operation
                # None means the server's default, not an invented client limit.
                for argument, attribute in (
                    ("limit", "db.vector.query.top_k"),
                    ("certainty", "db.vector.query.certainty"),
                    ("distance", "db.vector.query.distance"),
                    ("alpha", "db.vector.query.hybrid.alpha"),
                ):
                    value = kwargs.get(argument)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        attributes[attribute] = value
                if "db.vector.query.top_k" in attributes:
                    attributes[SpanAttributes.GEN_AI_RETRIEVAL_TOP_K] = attributes[
                        "db.vector.query.top_k"
                    ]
            elif operation == "insert":
                attributes["db.vector.upsert.count"] = 1
            elif operation == "insert_many":
                objects = args[0] if args else kwargs.get("objects")
                if isinstance(objects, (list, tuple)):
                    attributes["db.vector.upsert.count"] = len(objects)
        except Exception:
            # A tracing-only property must never stop a database call.
            _log_telemetry_failure("metadata capture")
        return attributes

    @contextmanager
    def _span(self, instance, args, kwargs):
        try:
            span = self._tracer.start_span(
                f"weaviate {self._operation}",
                kind=SpanKind.CLIENT,
                attributes=self._attributes(instance, args, kwargs),
                record_exception=False,
                set_status_on_exception=False,
            )
        except Exception:
            _log_telemetry_failure("span creation")
            yield INVALID_SPAN
            return
        with use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                self._input(span, args, kwargs)
                yield span
            except BaseException as exc:
                # Also finish cancellations, with exactly one privacy-filtered event.
                try:
                    span.record_exception(exc, escaped=True)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                except Exception:
                    _log_telemetry_failure("exception recording")
                raise
            finally:
                try:
                    span.end()
                except Exception:
                    _log_telemetry_failure("span completion")

    def _input(self, span, args, kwargs):
        if not self._capture_content or not span.is_recording():
            return
        if (
            self._config.hide_inputs
            or self._config.hide_input_text
            or self._config.hide_input_messages
        ):
            return
        if self._operation in _TEXT_QUERIES:
            try:
                query = kwargs.get("query", args[0] if args else None)
                if isinstance(query, str):
                    text = query
                elif isinstance(query, list) and all(
                    isinstance(item, str) for item in query
                ):
                    text = json.dumps(query, ensure_ascii=False)
                else:
                    return
                span.set_attribute(
                    SpanAttributes.GEN_AI_RETRIEVAL_QUERY, text[:_MAX_CONTENT_LENGTH]
                )
            except Exception:
                _log_telemetry_failure("query capture")

    def _result(self, span, result: Any):
        if not span.is_recording():
            return
        try:
            if self._operation in _QUERIES:
                groups = getattr(result, "groups", None)
                objects = getattr(result, "objects", None)
                if isinstance(groups, dict):
                    # GroupByReturn also has an objects list. Count members of
                    # groups, not the number of groups or a partial flat list.
                    count = sum(len(group.objects) for group in groups.values())
                    span.set_attribute("db.vector.results.count", count)
                elif isinstance(objects, (list, tuple)):
                    span.set_attribute("db.vector.results.count", len(objects))
                if isinstance(objects, (list, tuple)):
                    self._documents(span, objects)
            elif self._operation == "delete_by_id" and isinstance(result, bool):
                span.set_attribute("db.vector.delete.count", int(result))
            elif self._operation == "delete_many":
                successful = getattr(result, "successful", None)
                if isinstance(successful, int):
                    span.set_attribute("db.vector.delete.count", successful)
            failed = 0
            if self._operation == "insert_many":
                errors = getattr(result, "errors", None)
                if isinstance(errors, dict):
                    failed = len(errors)
            elif self._operation == "delete_many":
                failed = getattr(result, "failed", 0)
            if isinstance(failed, int) and failed > 0:
                span.set_attribute("db.vector.errors.count", failed)
                span.set_attribute("error.type", "partial_failure")
                span.set_status(Status(StatusCode.ERROR))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception:
            # Malformed metadata or a telemetry processor cannot replace results.
            _log_telemetry_failure("result capture")

    def _documents(self, span, objects):
        if not self._capture_content:
            return
        if (
            self._config.hide_outputs
            or self._config.hide_output_text
            or self._config.hide_output_messages
        ):
            return
        for index, obj in enumerate(objects[:_MAX_DOCUMENTS]):
            try:
                properties = getattr(obj, "properties", None)
                if not isinstance(properties, dict):
                    continue
                content = json.dumps(properties, ensure_ascii=False)
                span.set_attribute(
                    f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{index}.document.content",
                    content[:_MAX_CONTENT_LENGTH],
                )
            except Exception:
                # Do not stringify arbitrary objects, vectors, references or errors.
                _log_telemetry_failure("document capture")

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        with self._span(instance, args, kwargs) as span:
            result = wrapped(*args, **kwargs)
            self._result(span, result)
            return result

    async def async_call(
        self,
        wrapped: Callable[..., Awaitable[Any]],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        with self._span(instance, args, kwargs) as span:
            result = await wrapped(*args, **kwargs)
            self._result(span, result)
            return result
