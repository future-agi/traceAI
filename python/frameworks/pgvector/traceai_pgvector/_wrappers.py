"""pgvector method wrappers for OpenTelemetry instrumentation."""

import re
import logging
from typing import Any, Callable, Optional, Tuple

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

# FI canonical span-kind / IO keys. Optional dependency.
try:
    from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes

    _FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
    _FI_INPUT_VALUE = SpanAttributes.INPUT_VALUE
    _FI_INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
    _FI_RETRIEVER = FiSpanKindValues.RETRIEVER.value
except Exception:  # pragma: no cover
    _FI_SPAN_KIND = "gen_ai.span.kind"
    _FI_INPUT_VALUE = "input.value"
    _FI_INPUT_MIME_TYPE = "input.mime_type"
    _FI_RETRIEVER = "RETRIEVER"

logger = logging.getLogger(__name__)

# Vector operators in pgvector
VECTOR_OPERATORS = {
    "<->": "l2_distance",
    "<=>": "cosine_distance",
    "<#>": "inner_product",
    "<+>": "l1_distance",
}

# Regex patterns for detecting vector operations
VECTOR_OP_PATTERN = re.compile(r"(<->|<=>|<#>|<\+>)")
ORDER_BY_PATTERN = re.compile(r"ORDER\s+BY\s+.*?(<->|<=>|<#>|<\+>)", re.IGNORECASE)
LIMIT_PATTERN = re.compile(r"LIMIT\s+(\d+)", re.IGNORECASE)
TABLE_PATTERN = re.compile(r"(?:FROM|INTO|UPDATE)\s+(\w+)", re.IGNORECASE)


def detect_vector_operation(query) -> Optional[Tuple[str, dict]]:
    """Detect if a query contains pgvector operations and extract metadata."""
    if not query:
        return None

    # Handle psycopg3 Composed objects and other non-string query types
    if hasattr(query, 'as_string'):
        try:
            # Composed.as_string() can work with None for simple cases
            query = query.as_string(None)
        except Exception:
            # Fall back to str() if as_string fails
            query = str(query)
    elif not isinstance(query, str):
        query = str(query)

    query_upper = query.upper()

    # Check for vector operators
    vector_match = VECTOR_OP_PATTERN.search(query)
    if not vector_match:
        return None

    operator = vector_match.group(1)
    distance_type = VECTOR_OPERATORS.get(operator, "unknown")

    metadata = {
        "db.vector.distance_type": distance_type,
        "db.vector.operator": operator,
    }

    # Check if it's a similarity search (ORDER BY with vector operator)
    order_match = ORDER_BY_PATTERN.search(query)
    if order_match:
        metadata["db.vector.query.type"] = "similarity_search"

    # Extract limit
    limit_match = LIMIT_PATTERN.search(query)
    if limit_match:
        metadata["db.vector.query.top_k"] = int(limit_match.group(1))

    # Extract table name
    table_match = TABLE_PATTERN.search(query)
    if table_match:
        metadata["db.vector.collection.name"] = table_match.group(1)

    # Detect operation type
    if "INSERT" in query_upper:
        metadata["db.operation.name"] = "insert"
    elif "UPDATE" in query_upper:
        metadata["db.operation.name"] = "update"
    elif "DELETE" in query_upper:
        metadata["db.operation.name"] = "delete"
    elif "SELECT" in query_upper:
        metadata["db.operation.name"] = "query"
    else:
        metadata["db.operation.name"] = "unknown"

    return distance_type, metadata


class BaseWrapper:
    def __init__(self, tracer: Tracer):
        self._tracer = tracer

    def _get_attributes(self) -> dict:
        return {
            "db.system": "postgresql",
            "db.vector.extension": "pgvector",
        }


def _is_pgvector_query_op(metadata: dict) -> bool:
    """Return True iff this pgvector SQL is a similarity-search read."""
    return metadata.get("db.operation.name") == "query"


def _truncate_sql(sql: str, limit: int = 2000) -> str:
    return sql if len(sql) <= limit else sql[:limit] + "...[truncated]"


class ExecuteWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        query = args[0] if args else ""

        # Check if this is a vector operation
        vector_info = detect_vector_operation(query)

        if vector_info:
            distance_type, metadata = vector_info
            attributes = self._get_attributes()
            attributes.update(metadata)

            # FI canonical retriever attributes — only on similarity-search
            # reads (i.e. SELECT ... ORDER BY embedding <-> :query). Insert /
            # update / delete vector ops aren't retrievals.
            if _is_pgvector_query_op(metadata):
                attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
                # Surface the SQL itself as input.value; the dashboard's
                # Input panel renders this as the retrieval query.
                sql_text = (
                    query.as_string(None)
                    if hasattr(query, "as_string")
                    else str(query)
                )
                attributes[_FI_INPUT_VALUE] = _truncate_sql(sql_text)
                attributes[_FI_INPUT_MIME_TYPE] = "text/plain"

            span_name = f"pgvector {metadata.get('db.operation.name', 'query')}"

            with self._tracer.start_as_current_span(span_name, kind=SpanKind.CLIENT, attributes=attributes) as span:
                try:
                    result = wrapped(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise
        else:
            # Not a vector operation, just execute normally
            return wrapped(*args, **kwargs)


class ExecuteManyWrapper(BaseWrapper):
    def __call__(self, wrapped: Callable, instance: Any, args: tuple, kwargs: dict) -> Any:
        query = args[0] if args else ""
        params_seq = args[1] if len(args) > 1 else kwargs.get("vars_list", [])

        # Check if this is a vector operation
        vector_info = detect_vector_operation(query)

        if vector_info:
            distance_type, metadata = vector_info
            attributes = self._get_attributes()
            attributes.update(metadata)
            attributes["db.vector.batch.size"] = len(params_seq) if params_seq else 0

            # FI canonical retriever attributes — only on similarity-search
            # reads. Batched inserts/updates/deletes are not retrievals.
            if _is_pgvector_query_op(metadata):
                attributes[_FI_SPAN_KIND] = _FI_RETRIEVER
                sql_text = (
                    query.as_string(None)
                    if hasattr(query, "as_string")
                    else str(query)
                )
                attributes[_FI_INPUT_VALUE] = _truncate_sql(sql_text)
                attributes[_FI_INPUT_MIME_TYPE] = "text/plain"

            span_name = f"pgvector {metadata.get('db.operation.name', 'batch')}"

            with self._tracer.start_as_current_span(span_name, kind=SpanKind.CLIENT, attributes=attributes) as span:
                try:
                    result = wrapped(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise
        else:
            return wrapped(*args, **kwargs)
