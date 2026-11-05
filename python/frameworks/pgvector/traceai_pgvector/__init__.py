"""pgvector enrichment over the maintained OpenTelemetry PostgreSQL drivers."""

import importlib
import logging
from collections.abc import Collection
from typing import Any

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import unwrap
from wrapt import wrap_function_wrapper

from traceai_pgvector._privacy import DriverTracerProvider
from traceai_pgvector._wrappers import enrich_cursor_factory
from traceai_pgvector.package import _instruments
from traceai_pgvector.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class PgVectorInstrumentor(BaseInstrumentor):
    """Instrument new psycopg2 and psycopg3 sync/async connections.

    Install before opening connections. Do not enable a separate OTel psycopg
    instrumentor: this entry point owns that instrumentation and adds metadata
    to its single database span. SQL, parameters and error details are omitted.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        drivers = []
        for module_name, class_name in (
            ("psycopg2", "Psycopg2Instrumentor"),
            ("psycopg", "PsycopgInstrumentor"),
        ):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            implementation = importlib.import_module(
                "opentelemetry.instrumentation." + module_name
            )
            driver = getattr(implementation, class_name)()
            if driver.is_instrumented_by_opentelemetry:
                raise RuntimeError(
                    f"{class_name} is already enabled; disable it before enabling "
                    "PgVectorInstrumentor to avoid duplicate database spans."
                )
            targets = [(module, "connect", False)]
            if module_name == "psycopg":
                targets.extend(
                    [
                        (module.Connection, "connect", False),
                        (module.AsyncConnection, "connect", True),
                    ]
                )
            drivers.append((driver, targets))
        if not drivers:
            raise RuntimeError("Install psycopg2-binary or psycopg[binary] first.")

        self._provider = DriverTracerProvider(
            kwargs.get("tracer_provider") or trace.get_tracer_provider()
        )
        self._drivers = []
        self._connections = []
        try:
            for driver, targets in drivers:
                driver.instrument(
                    tracer_provider=self._provider,
                    capture_parameters=False,
                    enable_commenter=False,
                )
                if not driver.is_instrumented_by_opentelemetry:
                    raise RuntimeError(f"Could not enable {type(driver).__name__}")
                self._drivers.append(driver)
                for target, name, asynchronous in targets:
                    wrapper = self._connection_wrapper(asynchronous)
                    wrap_function_wrapper(target, name, wrapper)
                    self._connections.append((target, name))
        except Exception:
            self._uninstrument()
            raise

    def _connection_wrapper(self, asynchronous):
        # Capture this installation's provider. Re-enabling must not reactivate
        # old cursors or change the provider they export to.
        provider = self._provider

        def enrich(connection):
            connection.cursor_factory = enrich_cursor_factory(
                connection.cursor_factory,
                provider.is_enabled,
                asynchronous=asynchronous,
            )
            return connection

        if asynchronous:

            async def connect(wrapped, instance, args, kwargs):
                return enrich(await wrapped(*args, **kwargs))
        else:

            def connect(wrapped, instance, args, kwargs):
                return enrich(wrapped(*args, **kwargs))

        return connect

    def _uninstrument(self, **kwargs: Any) -> None:
        self._provider.disable()
        for target, name in reversed(self._connections):
            unwrap(target, name)
        self._connections.clear()
        for driver in reversed(self._drivers):
            driver.uninstrument()
        self._drivers.clear()


__all__ = ["PgVectorInstrumentor", "__version__"]
