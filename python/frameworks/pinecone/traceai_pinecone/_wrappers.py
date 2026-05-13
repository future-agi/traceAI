"""
Pinecone method wrappers for OpenTelemetry instrumentation.
"""

import json
import logging
from typing import Any, Callable, Optional

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

from traceai_pinecone._attributes import (
    VectorDBAttributes as Attrs,
    get_common_attributes,
    safe_json_dumps,
)

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


class BaseWrapper:
    """Base wrapper class for Pinecone operations."""

    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_index_name(self, instance: Any) -> Optional[str]:
        """Extract index name from Pinecone Index instance."""
        try:
            # Pinecone v3+ stores index name in _config or as attribute
            if hasattr(instance, "_config"):
                return getattr(instance._config, "index_name", None)
            if hasattr(instance, "name"):
                return instance.name
            if hasattr(instance, "_index_name"):
                return instance._index_name
            # Try to get from host
            if hasattr(instance, "_config") and hasattr(instance._config, "host"):
                host = instance._config.host
                # Extract index name from host like "index-name-xxx.svc.pinecone.io"
                if host:
                    return host.split(".")[0].rsplit("-", 1)[0]
        except Exception:
            pass
        return "unknown"

    def _get_index_host(self, instance: Any) -> Optional[str]:
        """Extract index host from Pinecone Index instance."""
        try:
            if hasattr(instance, "_config") and hasattr(instance._config, "host"):
                return instance._config.host
        except Exception:
            pass
        return None


class QueryWrapper(BaseWrapper):
    """Wrapper for Pinecone Index.query() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        index_name = self._get_index_name(instance)
        namespace = kwargs.get("namespace", "")
        top_k = kwargs.get("top_k", 10)
        include_metadata = kwargs.get("include_metadata", False)
        include_values = kwargs.get("include_values", False)
        filter_dict = kwargs.get("filter")

        span_name = "pinecone query"

        attributes = get_common_attributes("pinecone", "query", index_name)
        attributes.update({
            Attrs.QUERY_TOP_K: top_k,
            Attrs.QUERY_INCLUDE_METADATA: include_metadata,
            Attrs.QUERY_INCLUDE_VECTORS: include_values,
            Attrs.NAMESPACE: namespace or "",
        })

        if filter_dict:
            attributes[Attrs.QUERY_FILTER] = safe_json_dumps(filter_dict)

        host = self._get_index_host(instance)
        if host:
            attributes[Attrs.INDEX_HOST] = host

        # FI canonical retriever attributes.
        attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
        query_id = kwargs.get("id")
        query_vector = kwargs.get("vector")
        input_summary = {
            "top_k": top_k,
            "namespace": namespace or None,
            "filter": filter_dict,
            "id": query_id,
        }
        if isinstance(query_vector, list):
            input_summary["vector_dim"] = len(query_vector)
        attributes[_FI_INPUT_VALUE] = safe_json_dumps(
            {k: v for k, v in input_summary.items() if v is not None}
        )
        attributes[_FI_INPUT_MIME_TYPE] = "application/json"

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                # Extract results info
                if result and hasattr(result, "matches"):
                    matches = result.matches
                    span.set_attribute(Attrs.RESULTS_COUNT, len(matches))

                    # Get top scores
                    if matches:
                        scores = [m.score for m in matches[:10] if hasattr(m, "score")]
                        if scores:
                            span.set_attribute(Attrs.RESULTS_SCORES, safe_json_dumps(scores))

                        # Get result IDs
                        ids = [m.id for m in matches[:10] if hasattr(m, "id")]
                        if ids:
                            span.set_attribute(Attrs.RESULTS_IDS, safe_json_dumps(ids))

                        # FI canonical output.value — surface matches with
                        # id/score (and metadata when requested) for the
                        # Output panel.
                        output_payload = []
                        for m in matches[:50]:
                            entry = {
                                "id": getattr(m, "id", None),
                                "score": getattr(m, "score", None),
                            }
                            md = getattr(m, "metadata", None)
                            if md:
                                entry["metadata"] = md
                            output_payload.append(entry)
                        if output_payload:
                            span.set_attribute(
                                _FI_OUTPUT_VALUE, safe_json_dumps(output_payload)
                            )
                            span.set_attribute(
                                _FI_OUTPUT_MIME_TYPE, "application/json"
                            )

                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class UpsertWrapper(BaseWrapper):
    """Wrapper for Pinecone Index.upsert() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        index_name = self._get_index_name(instance)
        namespace = kwargs.get("namespace", "")

        # Get vectors from args or kwargs
        vectors = args[0] if args else kwargs.get("vectors", [])
        vector_count = len(vectors) if vectors else 0

        # Try to get vector dimensions
        dimensions = None
        if vectors and len(vectors) > 0:
            first_vector = vectors[0]
            if isinstance(first_vector, dict) and "values" in first_vector:
                dimensions = len(first_vector["values"])
            elif hasattr(first_vector, "values"):
                dimensions = len(first_vector.values)

        span_name = "pinecone upsert"

        attributes = get_common_attributes("pinecone", "upsert", index_name)
        attributes.update({
            Attrs.UPSERT_COUNT: vector_count,
            Attrs.NAMESPACE: namespace or "",
        })

        if dimensions:
            attributes[Attrs.UPSERT_DIMENSIONS] = dimensions

        host = self._get_index_host(instance)
        if host:
            attributes[Attrs.INDEX_HOST] = host

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                # Extract upserted count from response
                if result and hasattr(result, "upserted_count"):
                    span.set_attribute("db.vector.upserted_count", result.upserted_count)

                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DeleteWrapper(BaseWrapper):
    """Wrapper for Pinecone Index.delete() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        index_name = self._get_index_name(instance)
        namespace = kwargs.get("namespace", "")
        ids = kwargs.get("ids", [])
        delete_all = kwargs.get("delete_all", False)
        filter_dict = kwargs.get("filter")

        span_name = "pinecone delete"

        attributes = get_common_attributes("pinecone", "delete", index_name)
        attributes.update({
            Attrs.NAMESPACE: namespace or "",
            Attrs.DELETE_ALL: delete_all,
        })

        if ids:
            attributes[Attrs.DELETE_COUNT] = len(ids)

        if filter_dict:
            attributes[Attrs.QUERY_FILTER] = safe_json_dumps(filter_dict)

        host = self._get_index_host(instance)
        if host:
            attributes[Attrs.INDEX_HOST] = host

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class FetchWrapper(BaseWrapper):
    """Wrapper for Pinecone Index.fetch() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        index_name = self._get_index_name(instance)
        namespace = kwargs.get("namespace", "")
        ids = args[0] if args else kwargs.get("ids", [])

        span_name = "pinecone fetch"

        attributes = get_common_attributes("pinecone", "fetch", index_name)
        attributes.update({
            Attrs.NAMESPACE: namespace or "",
        })

        if ids:
            attributes["db.vector.fetch.ids_count"] = len(ids)

        host = self._get_index_host(instance)
        if host:
            attributes[Attrs.INDEX_HOST] = host

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                # Extract fetched count
                if result and hasattr(result, "vectors"):
                    span.set_attribute(Attrs.RESULTS_COUNT, len(result.vectors))

                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class UpdateWrapper(BaseWrapper):
    """Wrapper for Pinecone Index.update() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        index_name = self._get_index_name(instance)
        namespace = kwargs.get("namespace", "")
        vector_id = args[0] if args else kwargs.get("id", "")
        set_metadata = kwargs.get("set_metadata")
        values = kwargs.get("values")

        span_name = "pinecone update"

        attributes = get_common_attributes("pinecone", "update", index_name)
        attributes.update({
            Attrs.NAMESPACE: namespace or "",
            "db.vector.update.id": str(vector_id),
        })

        if set_metadata:
            attributes["db.vector.update.has_metadata"] = True

        if values:
            attributes["db.vector.update.has_values"] = True
            attributes[Attrs.UPSERT_DIMENSIONS] = len(values)

        host = self._get_index_host(instance)
        if host:
            attributes[Attrs.INDEX_HOST] = host

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)
                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise


class DescribeIndexStatsWrapper(BaseWrapper):
    """Wrapper for Pinecone Index.describe_index_stats() method."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        index_name = self._get_index_name(instance)
        filter_dict = kwargs.get("filter")

        span_name = "pinecone describe_index_stats"

        attributes = get_common_attributes("pinecone", "describe_index_stats", index_name)

        if filter_dict:
            attributes[Attrs.QUERY_FILTER] = safe_json_dumps(filter_dict)

        host = self._get_index_host(instance)
        if host:
            attributes[Attrs.INDEX_HOST] = host

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                result = wrapped(*args, **kwargs)

                # Extract stats
                if result:
                    if hasattr(result, "total_vector_count"):
                        span.set_attribute("db.vector.stats.total_count", result.total_vector_count)
                    if hasattr(result, "dimension"):
                        span.set_attribute(Attrs.INDEX_DIMENSIONS, result.dimension)
                    if hasattr(result, "namespaces"):
                        span.set_attribute("db.vector.stats.namespace_count", len(result.namespaces))

                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise
