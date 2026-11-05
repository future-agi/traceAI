"""Future AGI destination for Mirascope's native OpenTelemetry spans.

No SDK patching or new spans: call configure(), then mirascope.ops.instrument_llm().
Only this destination is filtered. Other exporters still see the original spans.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from threading import RLock
from weakref import WeakKeyDictionary

from fi_instrumentation.instrumentation._errors import is_error_detail_key
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.otel import HTTPSpanExporter
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import (
    Link,
    ProxyTracerProvider,
    SpanContext,
    Status,
)

_LOG = logging.getLogger(__name__)
_SCOPES = frozenset({"mirascope.llm", "mirascope.ops"})
_METADATA = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.output.type",
        "gen_ai.request.temperature",
        "gen_ai.request.max_tokens",
        "gen_ai.request.top_p",
        "gen_ai.request.top_k",
        "gen_ai.request.frequency_penalty",
        "gen_ai.request.presence_penalty",
        "gen_ai.request.seed",
        "gen_ai.request.choice_count",
        "gen_ai.response.model",
        "gen_ai.response.id",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "mirascope.response.usage",
        "mirascope.response.cost",
        "mirascope.response.model_id",
        "mirascope.response.provider_id",
        "error.type",
        "gen_ai.tool.name",
        "mirascope.type",
        "mirascope.fn.qualname",
        "mirascope.fn.module",
        "mirascope.fn.is_async",
        "mirascope.ops.session.id",
    }
)
_CONTENT_HIDE_FLAGS = (
    "hide_inputs",
    "hide_outputs",
    "hide_input_messages",
    "hide_output_messages",
    "hide_input_text",
    "hide_output_text",
    "hide_input_images",
    "hide_llm_invocation_parameters",
)
_lock = RLock()
_handles: WeakKeyDictionary = WeakKeyDictionary()


def _context(context):
    # IDs and sampling flags preserve parentage; arbitrary vendor tracestate does not.
    if context is None:
        return None
    return SpanContext(
        context.trace_id, context.span_id, context.is_remote, context.trace_flags
    )


def _is_stack(key: str) -> bool:
    return "stacktrace" in key.lower().replace("_", "").replace(".", "")


class MirascopeSpanExporter(SpanExporter):
    """Allowlist metadata, map native attributes, and export destination-local copies.

    Span/function/tool names and session IDs are application-defined metadata: keep
    private data out of them. Content opt-in includes unknown span attributes, but
    not stacks, scope/resource attributes, link attributes or non-exception events.
    Any FI_HIDE_* content setting conservatively disables content on this route.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        project_name: str,
        service_name: str = "mirascope-app",
        capture_content: bool = False,
    ):
        if not isinstance(capture_content, bool):
            raise TypeError("capture_content must be a bool")
        for name, value in (
            ("project_name", project_name),
            ("service_name", service_name),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        self._exporter = exporter
        self._config = TraceConfig()
        self._capture = capture_content and not any(
            getattr(self._config, name) for name in _CONTENT_HIDE_FLAGS
        )
        self._errors = self._capture and not self._config.hide_error_details
        # Do not inherit OTEL_RESOURCE_ATTRIBUTES (host/user data can live there).
        self._resource = Resource(
            {
                "project_name": project_name,
                "project_type": "observe",
                "service.name": service_name,
            }
        )

    def _copy(self, span: ReadableSpan) -> ReadableSpan:
        attributes = {}
        for key, value in (span.attributes or {}).items():
            if _is_stack(key) or (not self._errors and is_error_detail_key(key)):
                continue
            if (
                self._config.hide_llm_invocation_parameters
                and key.startswith("gen_ai.request.")
                and key != "gen_ai.request.model"
            ):
                continue
            if self._capture or key in _METADATA:
                masked = self._config.mask(key, value)
                if masked is not None:
                    attributes[key] = masked
        operation = attributes.get("gen_ai.operation.name")
        attributes["gen_ai.span.kind"] = (
            "LLM"
            if operation == "chat"
            else "TOOL"
            if operation == "execute_tool"
            else "CHAIN"
        )
        if "mirascope.ops.session.id" in attributes:
            attributes["session.id"] = attributes["mirascope.ops.session.id"]
        if self._capture:
            for target, sources in (
                (
                    "input.value",
                    ("gen_ai.input.messages", "mirascope.trace.arg_values"),
                ),
                ("output.value", ("gen_ai.output.messages", "mirascope.trace.output")),
            ):
                for source in sources:
                    if source in attributes:
                        attributes.setdefault(target, attributes[source])
                        break
        events = []
        for event in span.events:
            # Native info/debug event names can themselves contain user text.
            if event.name != "exception":
                continue
            allowed = (
                {"exception.type", "exception.message"}
                if self._errors
                else {"exception.type"}
            )
            values = {}
            for key, value in (event.attributes or {}).items():
                if key in allowed:
                    masked = self._config.mask(key, value)
                    if masked is not None:
                        values[key] = masked
            events.append(Event("exception", values, event.timestamp))
        description = span.status.description if self._errors else None
        if description is not None:
            description = self._config.mask("exception.message", description)
        status = Status(span.status.status_code, description)
        scope = span.instrumentation_scope
        return ReadableSpan(
            name=span.name,
            context=_context(span.context),
            parent=_context(span.parent),
            resource=self._resource,
            attributes=attributes,
            events=events,
            links=[Link(_context(link.context)) for link in span.links],
            kind=span.kind,
            status=status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=InstrumentationScope(scope.name, scope.version)
            if scope
            else None,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            copies = [
                self._copy(span)
                for span in spans
                if span.instrumentation_scope
                and span.instrumentation_scope.name in _SCOPES
            ]
            return self._exporter.export(copies) if copies else SpanExportResult.SUCCESS
        except Exception:  # noqa: BLE001 -- telemetry must fail closed without exposing payloads
            # Never fall back to the original batch or log exception payloads.
            _LOG.warning("Mirascope export failed; batch dropped, details redacted")
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._exporter.shutdown()


class MirascopeTracing(SpanProcessor):
    """A single destination, not ownership of the shared tracer provider.

    Shutdown is idempotent and closes only this destination. Configure once before
    starting application work; changing destination/privacy options or restarting
    this destination after shutdown requires a new process.
    """

    def __init__(self, processor, signature):
        self._processor = processor
        self._signature = signature
        self._closed = False
        self._lock = RLock()

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        # The SDK batch processor synchronizes shutdown with its enqueue operation.
        if (
            not self._closed
            and span.instrumentation_scope
            and span.instrumentation_scope.name in _SCOPES
        ):
            self._processor.on_end(span)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True if self._closed else self._processor.force_flush(timeout_millis)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._processor.shutdown()


def configure(
    *,
    project_name: str | None = None,
    service_name: str = "mirascope-app",
    capture_content: bool | None = None,
    tracer_provider: TracerProvider | None = None,
    endpoint: str | None = None,
    headers: dict[str, str] | None = None,
    exporter: SpanExporter | None = None,
) -> MirascopeTracing:
    """Configure native Mirascope 2.x tracing with a private Future AGI destination.

    Reuses the global SDK provider (or installs one when none exists). An explicit
    provider must be the global provider or be supplied before it is set. Enable
    ops.instrument_llm() separately; do not also instrument the underlying SDK.
    An injected exporter transfers ownership to the returned handle on success.
    Credentials/default endpoint follow fi_instrumentation.otel.HTTPSpanExporter.
    """
    from mirascope import ops

    project_name = (
        project_name if project_name is not None else os.getenv("FI_PROJECT_NAME")
    )
    if not isinstance(project_name, str) or not project_name.strip():
        raise ValueError("set project_name or FI_PROJECT_NAME")
    if not isinstance(service_name, str) or not service_name.strip():
        raise ValueError("service_name must be a nonempty string")
    if capture_content is None:
        capture_content = os.getenv("FI_CAPTURE_CONTENT", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if not isinstance(capture_content, bool):
        raise TypeError("capture_content must be a bool")
    if exporter is not None and (endpoint is not None or headers is not None):
        raise ValueError("use exporter or endpoint/headers, not both")
    if exporter is not None and not all(
        callable(getattr(exporter, name, None))
        for name in ("export", "force_flush", "shutdown")
    ):
        raise TypeError("exporter must implement export, force_flush and shutdown")
    if endpoint is not None and (not isinstance(endpoint, str) or not endpoint):
        raise ValueError("endpoint must be a nonempty URL")
    if headers is not None and (
        not isinstance(headers, dict)
        or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        )
    ):
        raise TypeError("headers must be a string-to-string dictionary")
    config = TraceConfig()
    signature = (
        project_name,
        service_name,
        capture_content,
        endpoint,
        tuple(sorted((headers or {}).items())),
        id(exporter),
        repr(config),
        tuple(
            os.getenv(name) for name in ("FI_BASE_URL", "FI_API_KEY", "FI_SECRET_KEY")
        )
        if exporter is None
        else (),
    )
    with _lock:
        current = trace.get_tracer_provider()
        provider = tracer_provider if tracer_provider is not None else current
        if isinstance(provider, ProxyTracerProvider):
            provider = TracerProvider()
        if not isinstance(provider, TracerProvider):
            raise TypeError("Mirascope requires an OpenTelemetry SDK TracerProvider")
        if not isinstance(current, ProxyTracerProvider) and current is not provider:
            raise ValueError(
                "tracer_provider must be the global provider used by Mirascope ops"
            )
        if getattr(provider, "_default_processor", False):
            raise ValueError(
                "use a plain SDK provider, not register()'s automatic export provider"
            )
        existing = _handles.get(provider)
        if existing is not None:
            if existing._closed:
                raise RuntimeError(
                    "Mirascope destination is shut down; start a new process"
                )
            if existing._signature != signature:
                raise ValueError(
                    "Mirascope destination is already configured with different options"
                )
            return existing
        # Configure before creating a network exporter so a missing ops extra fails cleanly.
        ops.configure(tracer_provider=provider)
        if trace.get_tracer_provider() is not provider:
            raise RuntimeError("another library installed a different global provider")
        inner = (
            exporter
            if exporter is not None
            else HTTPSpanExporter(
                endpoint=endpoint,
                headers=headers,
                compression=Compression.NoCompression,
            )
        )
        processor = BatchSpanProcessor(
            MirascopeSpanExporter(
                inner,
                project_name=project_name,
                service_name=service_name,
                capture_content=capture_content,
            )
        )
        handle = MirascopeTracing(processor, signature)
        try:
            provider.add_span_processor(handle)
        except BaseException:
            handle.shutdown()
            raise
        _handles[provider] = handle
        return handle


__all__ = ["MirascopeSpanExporter", "MirascopeTracing", "configure"]
