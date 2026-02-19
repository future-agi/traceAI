"""
Attribute constants for Pinecone vector database instrumentation.

Follows OpenTelemetry semantic conventions for databases with vector-specific extensions.
"""

import json
from typing import Any, Dict, Optional


class VectorDBAttributes:
    """
    Semantic conventions for vector database operations.

    Based on OpenTelemetry database semantic conventions with extensions
    for vector-specific operations.
    """

    # Core DB attributes (OTEL standard)
    DB_SYSTEM = "db.system"
    DB_OPERATION_NAME = "db.operation.name"
    DB_NAMESPACE = "db.namespace"

    # Query attributes
    QUERY_TOP_K = "db.vector.query.top_k"
    QUERY_FILTER = "db.vector.query.filter"
    QUERY_INCLUDE_METADATA = "db.vector.query.include_metadata"
    QUERY_INCLUDE_VECTORS = "db.vector.query.include_vectors"
    QUERY_SCORE_THRESHOLD = "db.vector.query.score_threshold"
    QUERY_METRIC = "db.vector.query.metric"

    # Result attributes
    RESULTS_COUNT = "db.vector.results.count"
    RESULTS_SCORES = "db.vector.results.scores"
    RESULTS_IDS = "db.vector.results.ids"

    # Upsert/Insert attributes
    UPSERT_COUNT = "db.vector.upsert.count"
    UPSERT_DIMENSIONS = "db.vector.upsert.dimensions"

    # Delete attributes
    DELETE_COUNT = "db.vector.delete.count"
    DELETE_ALL = "db.vector.delete.all"

    # Index/Collection attributes
    INDEX_NAME = "db.vector.index.name"
    INDEX_HOST = "db.vector.index.host"
    INDEX_METRIC = "db.vector.index.metric"
    INDEX_DIMENSIONS = "db.vector.index.dimensions"
    INDEX_TYPE = "db.vector.index.type"

    # Namespace/Partition
    NAMESPACE = "db.vector.namespace"
    PARTITION = "db.vector.partition"


class VectorDBOperations:
    """Standard operation names for vector databases."""

    # Query operations
    QUERY = "query"
    SEARCH = "search"
    SEARCH_BATCH = "search_batch"
    HYBRID_SEARCH = "hybrid_search"
    RECOMMEND = "recommend"
    GET = "get"
    FETCH = "fetch"
    SCROLL = "scroll"

    # Write operations
    INSERT = "insert"
    UPSERT = "upsert"
    UPDATE = "update"
    ADD = "add"

    # Delete operations
    DELETE = "delete"

    # Stats/Info operations
    COUNT = "count"
    DESCRIBE = "describe"
    DESCRIBE_INDEX_STATS = "describe_index_stats"
    LIST = "list"


def get_common_attributes(
    db_system: str,
    operation: str,
    index_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get common attributes for a vector DB operation.

    Args:
        db_system: Database system name (e.g., 'pinecone', 'chroma')
        operation: Operation name (e.g., 'query', 'upsert')
        index_name: Optional index or collection name

    Returns:
        Dictionary of span attributes
    """
    attributes = {
        VectorDBAttributes.DB_SYSTEM: db_system,
        VectorDBAttributes.DB_OPERATION_NAME: operation,
    }

    if index_name:
        attributes[VectorDBAttributes.DB_NAMESPACE] = index_name
        attributes[VectorDBAttributes.INDEX_NAME] = index_name

    return attributes


def safe_json_dumps(obj: Any) -> str:
    """
    Safely convert object to JSON string for span attributes.

    Args:
        obj: Object to serialize

    Returns:
        JSON string or string representation if serialization fails
    """
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return str(obj)
