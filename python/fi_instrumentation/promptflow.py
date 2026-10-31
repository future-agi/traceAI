"""Export Prompt Flow's native OpenTelemetry spans, without another instrumentor.

``configure`` owns the process-wide provider; call it once before running a flow.
Prompt Flow 1.18.x owns function, flow, OpenAI and streaming span lifecycles.
Content capture is opt-in. Privacy applies to native ``promptflow`` spans on this
export path, not to other exporters or arbitrary application instrumentation.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from threading import Lock

from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.otel import HTTPSpanExporter, register
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status

__all__ = ["PromptFlowSpanExporter", "configure"]

_KINDS = {
    "Flow": "CHAIN",
    "Function": "CHAIN",
    "LangChain": "CHAIN",
    "LLM": "LLM",
    "Embedding": "EMBEDDING",
    "Retrieval": "RETRIEVER",
}
_TOKENS = {
    "llm.usage.prompt_tokens": "llm.token_count.prompt",
    "llm.usage.completion_tokens": "llm.token_count.completion",
    "llm.usage.total_tokens": "llm.token_count.total",
}
_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:]{0,127}$")
_SETUP_LOCK = Lock()


class PromptFlowSpanExporter(SpanExporter):
    """Map native spans to Future AGI attributes before delegating export.

    Keeps span IDs, parentage, timestamps and status codes. Drops raw payload
    aliases, events and unknown native attributes; optionally maps inputs and
    outputs once, respecting ``TraceConfig`` (including environment settings).
    Cumulative token counts on parent flows are not exported as LLM usage.
    The wrapped exporter is owned by this wrapper and shut down with it.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        capture_content: bool = False,
        config: TraceConfig | None = None,
    ) -> None:
        self._exporter = exporter
        self._capture_content = capture_content
        self._config = config if config is not None else TraceConfig()

    def _convert(self, span: ReadableSpan) -> ReadableSpan:
        scope = span.instrumentation_scope
        if scope is None or scope.name != "promptflow":
            return span

        source = span.attributes or {}
        kind = _KINDS.get(source.get("span_type"), "CHAIN")
        attributes = {"fi.span.kind": kind, "framework": "promptflow"}
        if kind in ("LLM", "EMBEDDING"):
            model = source.get("llm.response.model")
            if isinstance(model, str):
                attributes["llm.model_name"] = model
            # Native parents contain cumulative totals, not additional calls.
            for native, target in _TOKENS.items():
                value = source.get(native)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    attributes[target] = value

        config = self._config
        if self._capture_content:
            # Serialized native fields cannot safely be redacted per message.
            # A directional hide setting therefore drops the whole field.
            hide_input = any(
                (
                    config.hide_inputs,
                    config.hide_input_messages,
                    config.hide_input_text,
                    config.hide_input_images,
                    config.hide_llm_invocation_parameters,
                )
            )
            hide_output = any(
                (
                    config.hide_outputs,
                    config.hide_output_messages,
                    config.hide_output_text,
                    kind == "EMBEDDING" and config.hide_embedding_vectors,
                )
            )
            for native, target, hidden in (
                ("inputs", "input", hide_input),
                ("output", "output", hide_output),
            ):
                value = source.get(native)
                if not hidden and isinstance(value, str):
                    masked = config.mask(f"{target}.value", value)
                    if masked is not None:
                        attributes[f"{target}.value"] = masked
                        attributes[f"{target}.mime_type"] = "application/json"

        # Native events repeat inputs/outputs and can carry exception text and
        # stack traces. Preserve only a bounded exception class identifier.
        for event in span.events or ():
            error_type = (event.attributes or {}).get("exception.type")
            if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type):
                attributes["error.type"] = error_type
        description = None
        if self._capture_content and not config.hide_error_details:
            description = config.mask("error.message", span.status.description)

        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=attributes,
            events=(),
            links=tuple(Link(link.context) for link in span.links or ()),
            kind=span.kind,
            status=Status(span.status.status_code, description),
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._exporter.export(tuple(self._convert(span) for span in spans))

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        self._exporter.shutdown()


def configure(
    *,
    project_name: str,
    capture_content: bool = False,
    config: TraceConfig | None = None,
    span_exporter: SpanExporter | None = None,
    batch: bool = True,
) -> TracerProvider:
    """Enable native Prompt Flow 1.18.x tracing with one Future AGI exporter.

    Call once per process, before other global OTel setup or OpenAI
    instrumentation. Returns the provider for ``force_flush``/``shutdown``.
    Uses ``FI_API_KEY``, ``FI_SECRET_KEY`` and optional ``FI_BASE_URL`` for HTTP
    export, or owns the explicitly supplied exporter. No local Prompt Flow
    service is started. No additional spans or SDK wrappers are implemented here.

    Enables ``PF_DISABLE_TRACING=false`` and disables Prompt Flow's separate
    local-service/exporter setup. Do not add ``OpenAIInstrumentor`` afterwards.
    For an already-owned provider, use ``PromptFlowSpanExporter`` in its sole
    export pipeline instead; privacy cannot protect another raw exporter.
    """
    with _SETUP_LOCK:
        if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            raise RuntimeError(  # noqa: TRY004 - invalid setup order, not an argument type
                "Configure Prompt Flow before setting a global tracer provider."
            )
        try:
            release = version("promptflow-tracing")
        except PackageNotFoundError as exc:
            raise ImportError(
                "Install promptflow-tracing>=1.18.0,<1.19 for native tracing."
            ) from exc
        if not release.startswith("1.18."):
            raise ValueError(
                "Native Prompt Flow export supports promptflow-tracing 1.18.x."
            )
        # Detect the two common competing wrappers without importing/instrumenting
        # optional packages. Prompt Flow itself makes its native injection idempotent.
        for module_name in ("traceai_openai", "opentelemetry.instrumentation.openai"):
            module = sys.modules.get(module_name)
            instrumentor = getattr(module, "OpenAIInstrumentor", None)
            if (
                instrumentor is not None
                and instrumentor().is_instrumented_by_opentelemetry
            ):
                raise RuntimeError(
                    "Remove OpenAIInstrumentor before enabling native Prompt Flow tracing."
                )

        from promptflow.tracing._integrations._openai_injector import inject_openai_api

        exporter = PromptFlowSpanExporter(
            span_exporter if span_exporter is not None else HTTPSpanExporter(),
            capture_content=capture_content,
            config=config,
        )
        provider = register(
            project_name=project_name,
            project_type=ProjectType.OBSERVE,
            set_global_tracer_provider=True,
            span_exporter=exporter,
            batch=batch,
            verbose=False,
        )
        if trace.get_tracer_provider() is not provider:
            provider.shutdown()
            raise RuntimeError(
                "Another tracer provider was installed during Prompt Flow setup."
            )

        os.environ["PF_DISABLE_TRACING"] = "false"
        os.environ["PF_TRACING_SKIP_LOCAL_SETUP"] = "true"
        os.environ["PF_TRACING_SKIP_EXPORTER_SETUP"] = "true"
        # Delegate to Prompt Flow's own hook rather than instrumenting OpenAI twice.
        # Its public start_trace also starts a local service when devkit is installed.
        inject_openai_api()
        return provider
