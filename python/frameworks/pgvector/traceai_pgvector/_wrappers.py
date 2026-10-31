"""Metadata-only enrichment for spans owned by the OTel PostgreSQL drivers.

This is deliberately not a SQL parser. Only recognized SQL operations and
pgvector operators become attributes; query text, identifiers and values never
leave this module. Unknown queries still get their driver's database span.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

VECTOR_OPERATORS = {
    "<->": "l2_distance",
    "<=>": "cosine_distance",
    "<#>": "inner_product",
    "<+>": "l1_distance",
    "<~>": "hamming_distance",
    "<%>": "jaccard_distance",
}
VECTOR_OP_PATTERN = re.compile(r"<->|<=>|<#>|<\+>|<~>|<%>")
# Mask literals, quoted identifiers and comments before recognizing operators.
# Dollar-quoted strings and nested comments are handled by the scanner below.
_SQL_TOKEN = re.compile(
    r"--[^\n]*|/\*|\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$|"
    r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|[^\"])*\"",
    re.DOTALL,
)
_OPERATION = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|WITH|BEGIN|COMMIT|ROLLBACK)\b",
    re.IGNORECASE,
)
_QUERY_ATTRIBUTES = ContextVar("pgvector_query_attributes", default=None)


def _structure(query: str) -> str:
    """Remove SQL data before best-effort structural matching."""
    parts = []
    position = 0
    while match := _SQL_TOKEN.search(query, position):
        parts.append(query[position : match.start()])
        token = match.group()
        end = match.end()
        if token == "/*":
            depth = 1
            while depth and end < len(query):
                next_token = re.search(r"/\*|\*/", query[end:])
                if next_token is None:
                    end = len(query)
                    break
                depth += 1 if next_token.group() == "/*" else -1
                end += next_token.end()
        elif token.startswith("$"):
            closing = query.find(token, end)
            end = len(query) if closing < 0 else closing + len(token)
        parts.append(" ")
        position = end
    parts.append(query[position:])
    return "".join(parts)


def query_attributes(query: Any, cursor: Any = None) -> dict:
    """Read query metadata without consuming parameters or exporting SQL."""
    try:
        if isinstance(query, bytes):
            query = query.decode("utf-8", "replace")
        elif hasattr(query, "as_string"):
            query = query.as_string(cursor)
        if not isinstance(query, str):
            return {}
        # Bound enrichment work; the driver still receives the complete query.
        structure = _structure(query[:65536])
        attributes = {}
        operation = _OPERATION.match(structure)
        if operation:
            attributes["db.operation.name"] = operation.group(1).lower()
        vector = VECTOR_OP_PATTERN.search(structure)
        if vector:
            attributes.update(
                {
                    "db.vector.extension": "pgvector",
                    "db.vector.operator": vector.group(),
                    "db.vector.distance_type": VECTOR_OPERATORS[vector.group()],
                }
            )
            if re.search(
                r"ORDER\s+BY\b[^;]*" + re.escape(vector.group()),
                structure,
                re.IGNORECASE,
            ):
                attributes["db.vector.query.type"] = "similarity_search"
            limit = re.search(r"\bLIMIT\s+(\d+)\b", structure, re.IGNORECASE)
            if limit:
                value = int(limit.group(1))
                if value <= 2**63 - 1:
                    attributes["db.vector.query.top_k"] = value
        return attributes
    except Exception:  # noqa: BLE001 - metadata is best-effort; never block the driver
        # Fail open. Error strings/tracebacks can contain SQL and credentials,
        # so the diagnostic deliberately omits exception details.
        logging.getLogger(__name__).warning("Could not enrich pgvector query metadata")
        return {}


def detect_vector_operation(query: Any) -> tuple[str, dict] | None:
    """Compatibility helper; only structural vector metadata is returned."""
    attributes = query_attributes(query)
    distance = attributes.get("db.vector.distance_type")
    return (distance, attributes) if distance else None


def enrich_cursor_factory(
    base: type, enabled: Callable[[], bool], asynchronous: bool = False
) -> type:
    """Add context to a maintained driver cursor, without creating a span."""

    @contextmanager
    def query_context(cursor, args, kwargs):
        if not enabled():
            yield
            return
        # psycopg2 and psycopg3 both name the SQL argument `query`.
        query = args[0] if args else kwargs.get("query")
        token = _QUERY_ATTRIBUTES.set(query_attributes(query, cursor))
        try:
            yield
        finally:
            _QUERY_ATTRIBUTES.reset(token)

    class Cursor(base):
        def execute(self, *args, **kwargs):
            with query_context(self, args, kwargs):
                return super().execute(*args, **kwargs)

        def executemany(self, *args, **kwargs):
            with query_context(self, args, kwargs):
                return super().executemany(*args, **kwargs)

    class AsyncCursor(base):
        async def execute(self, *args, **kwargs):
            with query_context(self, args, kwargs):
                return await super().execute(*args, **kwargs)

        async def executemany(self, *args, **kwargs):
            with query_context(self, args, kwargs):
                return await super().executemany(*args, **kwargs)

    return AsyncCursor if asynchronous else Cursor
