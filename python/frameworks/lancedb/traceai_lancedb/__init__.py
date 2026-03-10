"""
TraceAI LanceDB Instrumentation

Provides OpenTelemetry instrumentation for LanceDB vector database operations.
"""

import logging
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_lancedb.version import __version__
from traceai_lancedb.package import _instruments
from traceai_lancedb._wrappers import (
    SearchWrapper,
    AddWrapper,
    UpdateWrapper,
    DeleteWrapper,
    CreateTableWrapper,
    DropTableWrapper,
    OpenTableWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class LanceDBInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for LanceDB vector database."""

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()

        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        self._originals = {}

        try:
            # LanceDB table operations
            import lancedb
            from lancedb.table import LanceTable
            from lancedb.db import LanceDBConnection
            from lancedb.query import LanceVectorQueryBuilder

            # Table operations
            self._originals["add"] = LanceTable.add
            self._originals["update"] = LanceTable.update
            self._originals["delete"] = LanceTable.delete

            # Database operations
            self._originals["create_table"] = LanceDBConnection.create_table
            self._originals["drop_table"] = LanceDBConnection.drop_table
            self._originals["open_table"] = LanceDBConnection.open_table

            # Search operations - wrap the to_list/to_arrow methods of query builder
            self._originals["query_to_list"] = LanceVectorQueryBuilder.to_list
            self._originals["query_to_arrow"] = LanceVectorQueryBuilder.to_arrow

            wrap_function_wrapper("lancedb.table", "LanceTable.add", AddWrapper(tracer))
            wrap_function_wrapper("lancedb.table", "LanceTable.update", UpdateWrapper(tracer))
            wrap_function_wrapper("lancedb.table", "LanceTable.delete", DeleteWrapper(tracer))
            wrap_function_wrapper("lancedb.db", "LanceDBConnection.create_table", CreateTableWrapper(tracer))
            wrap_function_wrapper("lancedb.db", "LanceDBConnection.drop_table", DropTableWrapper(tracer))
            wrap_function_wrapper("lancedb.db", "LanceDBConnection.open_table", OpenTableWrapper(tracer))
            wrap_function_wrapper("lancedb.query", "LanceVectorQueryBuilder.to_list", SearchWrapper(tracer, "to_list"))
            wrap_function_wrapper("lancedb.query", "LanceVectorQueryBuilder.to_arrow", SearchWrapper(tracer, "to_arrow"))

            logger.debug("LanceDB instrumentation enabled")
        except ImportError as e:
            logger.warning(f"Could not instrument LanceDB: {e}")

    def _uninstrument(self, **kwargs: Any) -> None:
        pass


__all__ = ["LanceDBInstrumentor", "__version__"]
