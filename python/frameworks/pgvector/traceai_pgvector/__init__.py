"""
TraceAI pgvector Instrumentation

Provides OpenTelemetry instrumentation for pgvector PostgreSQL extension.
"""

import logging
from typing import Any, Collection

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import wrap_function_wrapper

from traceai_pgvector.version import __version__
from traceai_pgvector.package import _instruments
from traceai_pgvector._wrappers import (
    ExecuteWrapper,
    ExecuteManyWrapper,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class PgVectorInstrumentor(BaseInstrumentor):
    """OpenTelemetry instrumentor for pgvector PostgreSQL extension."""

    __slots__ = ("_originals",)

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()

        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider)

        self._originals = {}

        try:
            # Try psycopg (v3) first - it has better Python interop and can be wrapped
            try:
                import psycopg
                from psycopg import Cursor

                self._originals["psycopg_execute"] = Cursor.execute
                self._originals["psycopg_executemany"] = Cursor.executemany

                wrap_function_wrapper("psycopg", "Cursor.execute", ExecuteWrapper(tracer))
                wrap_function_wrapper("psycopg", "Cursor.executemany", ExecuteManyWrapper(tracer))
                logger.debug("pgvector instrumentation enabled (psycopg3)")
            except ImportError:
                logger.debug("psycopg3 not available")

            # Also try psycopg2 if available
            try:
                import psycopg2

                # Wrap connection.cursor to return instrumented cursors
                original_cursor = psycopg2.extensions.connection.cursor

                def instrumented_cursor(conn_self, *args, **kwargs):
                    cur = original_cursor(conn_self, *args, **kwargs)
                    # Wrap the cursor's execute methods using a proxy
                    original_execute = cur.execute
                    original_executemany = cur.executemany

                    execute_wrapper = ExecuteWrapper(tracer)
                    executemany_wrapper = ExecuteManyWrapper(tracer)

                    def wrapped_execute(*args, **kwargs):
                        return execute_wrapper(original_execute, cur, args, kwargs)

                    def wrapped_executemany(*args, **kwargs):
                        return executemany_wrapper(original_executemany, cur, args, kwargs)

                    cur.execute = wrapped_execute
                    cur.executemany = wrapped_executemany
                    return cur

                self._originals["connection_cursor"] = original_cursor
                psycopg2.extensions.connection.cursor = instrumented_cursor

                logger.debug("pgvector instrumentation enabled (psycopg2)")
            except ImportError:
                logger.debug("psycopg2 not available")
            except TypeError as e:
                logger.debug(f"Could not patch psycopg2 cursor (C extension limitation): {e}")

        except Exception as e:
            logger.warning(f"Could not instrument pgvector: {e}")

    def _uninstrument(self, **kwargs: Any) -> None:
        pass


__all__ = ["PgVectorInstrumentor", "__version__"]
