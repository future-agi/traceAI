"""ChromaDB method wrappers that leave application arguments and results intact."""

import inspect
import logging
from contextlib import contextmanager
from numbers import Real
from typing import Any, Optional

from opentelemetry import context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, StatusCode, Tracer

from traceai_chromadb._attributes import (
    VectorDBAttributes as Attrs,
    get_common_attributes,
    safe_json_dumps,
)

logger = logging.getLogger(__name__)
_ACTIVE_COLLECTION_CALL = context.create_key("traceai_chromadb_active_collection_call")


def _count(value: Any) -> int:
    """Count OneOrMany inputs without evaluating NumPy array truthiness."""
    if value is None:
        return 0
    if isinstance(value, (str, dict)):
        return 1
    return len(value)


def _embedding_shape(value: Any) -> tuple:
    if value is None or len(value) == 0:
        return 0, 0
    if isinstance(value[0], Real):
        return 1, len(value)
    return len(value), len(value[0])


def _filters(parameters: dict) -> dict:
    attributes = {}
    for parameter, attribute in (
        ("where", Attrs.QUERY_FILTER),
        ("where_document", "db.vector.query.where_document"),
        ("include", "db.vector.query.include"),
    ):
        value = parameters.get(parameter)
        if value is not None:
            attributes[attribute] = safe_json_dumps(value)
    return attributes


def _suppressed() -> bool:
    return bool(
        context.get_value(_SUPPRESS_INSTRUMENTATION_KEY)
        or context.get_value(_ACTIVE_COLLECTION_CALL)
    )


class BaseWrapper:
    operation = ""

    def __init__(self, tracer: Tracer, signature: Optional[inspect.Signature] = None):
        self._tracer = tracer
        self._signature = signature

    def _request_attributes(self, parameters: dict) -> dict:
        return {}

    def _result_attributes(self, result: Any) -> dict:
        return {}

    @contextmanager
    def _span(self, wrapped, instance, args, kwargs):
        attributes = get_common_attributes("chroma", self.operation)
        try:
            name = getattr(instance, "name", None)
            if name is not None:
                attributes.update(get_common_attributes("chroma", self.operation, name))
            signature = self._signature or inspect.signature(wrapped)
            parameters = signature.bind(*args, **kwargs)
            parameters.apply_defaults()
            attributes.update(self._request_attributes(parameters.arguments))
        except Exception:
            # Attribute collection must not reject a valid call, or replace the
            # framework's own validation error for an invalid one.
            logger.debug("Could not extract ChromaDB request attributes")
        with self._tracer.start_as_current_span(
            f"chroma {self.operation}", kind=SpanKind.CLIENT, attributes=attributes
        ) as span:
            # Suppress only nested collection wrappers, not embedding/transport spans.
            token = context.attach(context.set_value(_ACTIVE_COLLECTION_CALL, True))
            try:
                yield span
            finally:
                context.detach(token)

    def _finish(self, span, result):
        try:
            span.set_attributes(self._result_attributes(result))
        except Exception:
            logger.debug("Could not extract ChromaDB result attributes")
        span.set_status(StatusCode.OK)

    def __call__(self, wrapped, instance, args, kwargs):
        if _suppressed():
            return wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs) as span:
            result = wrapped(*args, **kwargs)
            self._finish(span, result)
            return result

    async def async_call(self, wrapped, instance, args, kwargs):
        if _suppressed():
            return await wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs) as span:
            result = await wrapped(*args, **kwargs)
            self._finish(span, result)
            return result


class AddWrapper(BaseWrapper):
    operation = "add"

    def _request_attributes(self, parameters):
        embedding_count, dimensions = _embedding_shape(parameters.get("embeddings"))
        attributes = {
            Attrs.UPSERT_COUNT: (
                _count(parameters.get("ids"))
                or _count(parameters.get("documents"))
                or embedding_count
            )
        }
        if dimensions:
            attributes[Attrs.UPSERT_DIMENSIONS] = dimensions
        for field in ("documents", "metadatas"):
            if parameters.get(field) is not None:
                attributes[f"db.vector.{field}.count"] = _count(parameters[field])
        return attributes


class UpsertWrapper(AddWrapper):
    operation = "upsert"


class UpdateWrapper(BaseWrapper):
    operation = "update"

    def _request_attributes(self, parameters):
        attributes = {"db.vector.update.count": _count(parameters.get("ids"))}
        for field in ("embeddings", "documents", "metadatas"):
            if parameters.get(field) is not None:
                attributes[f"db.vector.update.has_{field}"] = True
        return attributes


class QueryWrapper(BaseWrapper):
    operation = "query"
    score_key = "distances"

    def _request_attributes(self, parameters):
        attributes = _filters(parameters)
        attributes[Attrs.QUERY_TOP_K] = parameters.get("n_results", 10)
        if parameters.get("query_embeddings") is not None:
            count, _ = _embedding_shape(parameters["query_embeddings"])
            attributes["db.vector.query.type"] = "embedding"
            attributes["db.vector.query.embedding_count"] = count
        elif parameters.get("query_texts") is not None:
            attributes["db.vector.query.type"] = "text"
            attributes["db.vector.query.text_count"] = _count(parameters["query_texts"])
        return attributes

    def _result_attributes(self, result):
        attributes = {}
        # Preserve the existing first-query result summary and ten-item cap.
        ids = result.get("ids")
        if ids is not None and len(ids) > 0:
            attributes[Attrs.RESULTS_COUNT] = len(ids[0])
            if len(ids[0]) > 0:
                attributes[Attrs.RESULTS_IDS] = safe_json_dumps(ids[0][:10])
        scores = result.get(self.score_key)
        if scores is not None and len(scores) > 0 and scores[0] is not None:
            if len(scores[0]) > 0:
                attributes[Attrs.RESULTS_SCORES] = safe_json_dumps(scores[0][:10])
        return attributes


class SearchWrapper(QueryWrapper):
    """Chroma's newer Search API; do not serialize its rank/filter expressions."""

    operation = "search"
    score_key = "scores"

    def _request_attributes(self, parameters):
        searches = parameters.get("searches")
        return {
            "db.vector.search.count": len(searches) if isinstance(searches, list) else 1
        }


class GetWrapper(BaseWrapper):
    operation = "get"

    def _request_attributes(self, parameters):
        attributes = _filters(parameters)
        if parameters.get("ids") is not None:
            attributes["db.vector.get.ids_count"] = _count(parameters["ids"])
        for field in ("limit", "offset"):
            if parameters.get(field) is not None:
                attributes[f"db.vector.get.{field}"] = parameters[field]
        return attributes

    def _result_attributes(self, result):
        if result.get("ids") is not None:
            return {Attrs.RESULTS_COUNT: len(result["ids"])}
        return {}


class PeekWrapper(GetWrapper):
    operation = "peek"

    def _request_attributes(self, parameters):
        return {"db.vector.peek.limit": parameters.get("limit", 10)}


class DeleteWrapper(BaseWrapper):
    operation = "delete"

    def _request_attributes(self, parameters):
        attributes = _filters(parameters)
        if parameters.get("ids") is not None:
            attributes[Attrs.DELETE_COUNT] = _count(parameters["ids"])
        return attributes


class CountWrapper(BaseWrapper):
    operation = "count"

    def _result_attributes(self, result):
        if isinstance(result, int):
            return {"db.vector.collection.count": result}
        return {}
