"""Native Temporal provider setup and bounded, application-owned lifecycle."""

import atexit
import logging
import math
import os
import threading
from types import TracebackType
from urllib.parse import urlsplit

from fi_instrumentation.otel import BatchSpanProcessor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits
from opentelemetry.sdk.trace.export import SpanExporter
from temporalio.contrib.opentelemetry import (
    OpenTelemetryPlugin,
    ReplaySafeTracerProvider,
    create_tracer_provider,
)
from typing_extensions import Self

from ._privacy import SafeSpanExporter
from ._session import WorkflowIdAsSession

_CONFIGURE_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)


def _positive(value, name):
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


class TemporalTracing:
    """Provider, native plugin, and bounded flush/shutdown.

    Stop the worker before shutdown. A timeout returns False; it does not kill
    an in-progress exporter. At most one flush and one shutdown task run, and
    they are daemon threads so a broken exporter cannot hold process exit.
    OpenTelemetry does not permit replacing a shut-down global provider.
    """

    def __init__(
        self,
        provider: ReplaySafeTracerProvider,
        plugin: OpenTelemetryPlugin,
        shutdown_timeout_millis: int,
    ) -> None:
        self.provider = provider
        self.plugin = plugin
        self._timeout = shutdown_timeout_millis
        self._lock = threading.Lock()
        self._flush_thread: threading.Thread | None = None
        self._flush_result = False
        self._shutdown_thread: threading.Thread | None = None
        self._shutdown_result = False
        self._closed = False
        self._exit_hook = self.shutdown
        atexit.register(self._exit_hook)

    @property
    def closed(self) -> bool:
        return self._closed

    def force_flush(self, timeout_millis: int = 3000) -> bool:
        _positive(timeout_millis, "timeout_millis")
        with self._lock:
            if self._closed:
                return False
            if self._flush_thread is None or not self._flush_thread.is_alive():
                self._flush_result = False

                def flush():
                    try:
                        self._flush_result = bool(
                            self.provider.force_flush(timeout_millis)
                        )
                    except Exception:  # noqa: BLE001 -- lifecycle failures must not escape into the application
                        self._flush_result = False
                        # Exception text and stacks may contain credentials/content.
                        _LOGGER.warning(
                            "Temporal tracing flush failed; details redacted"
                        )

                self._flush_thread = threading.Thread(
                    target=flush, name="traceai-temporal-flush", daemon=True
                )
                self._flush_thread.start()
            thread = self._flush_thread
        thread.join(timeout_millis / 1000)
        return not thread.is_alive() and self._flush_result

    def shutdown(self, timeout_millis: int | None = None) -> bool:
        timeout_millis = self._timeout if timeout_millis is None else timeout_millis
        _positive(timeout_millis, "timeout_millis")
        with self._lock:
            if self._shutdown_thread is None:
                self._closed = True
                atexit.unregister(self._exit_hook)

                def stop():
                    try:
                        # BatchSpanProcessor.shutdown drains ended spans. Do not end
                        # open workflow spans here: replay safety belongs to Temporal.
                        self.provider.shutdown()
                        self._shutdown_result = True
                    except Exception:  # noqa: BLE001 -- lifecycle failures must not escape into the application
                        self._shutdown_result = False
                        _LOGGER.warning(
                            "Temporal tracing shutdown failed; details redacted"
                        )

                self._shutdown_thread = threading.Thread(
                    target=stop, name="traceai-temporal-shutdown", daemon=True
                )
                self._shutdown_thread.start()
            thread = self._shutdown_thread
        thread.join(timeout_millis / 1000)
        return not thread.is_alive() and self._shutdown_result

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.shutdown()


def configure(
    *,
    project_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    secret_key: str | None = None,
    capture_content: bool = False,
    keep_error_text: bool = False,
    compression: str = "none",
    exporter: SpanExporter | None = None,
    export_timeout: float = 3.0,
    shutdown_timeout_millis: int = 3000,
    max_queue_size: int = 2048,
    max_export_batch_size: int = 512,
    schedule_delay_millis: int = 1000,
    max_content_length: int = 16384,
) -> TemporalTracing:
    """Configure once, before Client.connect(), in the starter and the worker.

    Use handle.plugin on each client; workers inherit it from the client. The
    native provider is registered globally; an existing global SDK provider is
    rejected before an exporter is created. No model SDK is instrumented here.
    Pass this provider to the one traceAI model instrumentor your activity uses.
    Passing exporter bypasses endpoint/auth configuration and transfers ownership.
    Do not import this module from workflow code.
    """
    project_name = (
        project_name if project_name is not None else os.getenv("FI_PROJECT_NAME")
    )
    if not isinstance(project_name, str) or not project_name.strip():
        raise ValueError("Set project_name or FI_PROJECT_NAME")
    if not isinstance(capture_content, bool) or not isinstance(keep_error_text, bool):
        raise TypeError("capture_content and keep_error_text must be booleans")
    for value, name in (
        (export_timeout, "export_timeout"),
        (shutdown_timeout_millis, "shutdown_timeout_millis"),
        (schedule_delay_millis, "schedule_delay_millis"),
        (max_queue_size, "max_queue_size"),
        (max_export_batch_size, "max_export_batch_size"),
        (max_content_length, "max_content_length"),
    ):
        _positive(value, name)
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in (max_queue_size, max_export_batch_size, max_content_length)
    ):
        raise ValueError("Queue, batch, and content limits must be integers")
    if max_export_batch_size > max_queue_size:
        raise ValueError("max_export_batch_size must not exceed max_queue_size")
    if compression not in {"none", "gzip"}:
        raise ValueError("compression must be 'none' or 'gzip'")

    with _CONFIGURE_LOCK:
        if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            raise RuntimeError(  # noqa: TRY004 -- process state conflict, not a bad argument
                "A global tracer provider already exists; call configure() once, before other instrumentation"
            )
        if exporter is None:
            base_url = (
                base_url
                if base_url is not None
                else os.getenv("FI_BASE_URL", "https://api.futureagi.com")
            )
            if not isinstance(base_url, str):
                raise TypeError("base_url must be a string")
            parsed = urlsplit(base_url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "base_url must be an HTTP(S) origin or path, without credentials/query/fragment"
                )
            if parsed.scheme == "http" and parsed.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError(
                    "Remote endpoints require HTTPS; HTTP is allowed only on loopback"
                )
            api_key = api_key if api_key is not None else os.getenv("FI_API_KEY")
            secret_key = (
                secret_key if secret_key is not None else os.getenv("FI_SECRET_KEY")
            )
            if not api_key or not secret_key:
                raise ValueError(
                    "Set both FI_API_KEY and FI_SECRET_KEY, or pass api_key and secret_key"
                )
            exporter = OTLPSpanExporter(
                endpoint=base_url.rstrip("/") + "/tracer/v1/traces",
                headers={"X-Api-Key": api_key, "X-Secret-Key": secret_key},
                timeout=export_timeout,
                compression=Compression.Gzip
                if compression == "gzip"
                else Compression.NoCompression,
            )
        safe = SafeSpanExporter(
            exporter,
            capture_content=capture_content,
            keep_error_text=keep_error_text,
            max_content_length=max_content_length,
        )
        provider = create_tracer_provider(
            # Resource(), not create(): no environment, host, process, or SDK detectors.
            resource=Resource(
                {
                    "project_name": project_name,
                    "project_type": "observe",
                    "semantic_convention": "fi",
                    "service.name": "temporal",
                }
            ),
            shutdown_on_exit=False,
            span_limits=SpanLimits(
                max_attributes=128,
                max_events=64,
                max_links=64,
                max_attribute_length=max(16384, max_content_length),
            ),
        )
        provider.add_span_processor(WorkflowIdAsSession())
        provider.add_span_processor(
            BatchSpanProcessor(
                safe,
                max_queue_size=max_queue_size,
                max_export_batch_size=max_export_batch_size,
                schedule_delay_millis=schedule_delay_millis,
            )
        )
        handle = TemporalTracing(
            provider,
            OpenTelemetryPlugin(add_temporal_spans=True),
            shutdown_timeout_millis,
        )
        trace.set_tracer_provider(provider)
        if trace.get_tracer_provider() is not provider:
            # An unrelated library may race our lock; never return an unusable handle.
            handle.shutdown()
            raise RuntimeError(
                "Another library installed the global provider during Temporal configuration"
            )
        return handle
