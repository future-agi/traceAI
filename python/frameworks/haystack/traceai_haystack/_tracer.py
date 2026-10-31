"""Haystack's public tracing protocol, adapted to Future AGI semantics."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import time_ns
from traceback import walk_tb
from typing import Any, Iterator, Mapping, Optional

from fi_instrumentation import FITracer, TraceConfig
from haystack.tracing import Span, Tracer
from opentelemetry import trace as trace_api

from traceai_haystack import _semantics as sem
from traceai_haystack._bindings import pipeline_context

logger = logging.getLogger(__name__)


def _log_extraction_failure(message: str, exception: Exception) -> None:
    # Best-effort telemetry fails open. Keep diagnostic frames, but never log
    # exception text, source lines or locals: they may contain private payloads.
    frames = [
        (frame.f_code.co_name, line) for frame, line in walk_tb(exception.__traceback__)
    ]
    logger.debug("%s (%s; frames=%s)", message, type(exception).__name__, frames)


_active_span: ContextVar[Optional["HaystackSpan"]] = ContextVar(
    "traceai_haystack_span", default=None
)

# Native input_data is an ordinary tag, not a content tag. Never pass arbitrary
# Haystack tags through to OTel: they bypass FI's semantic privacy controls.
_SAFE_TAGS = frozenset(
    {
        "haystack.component.name",
        "haystack.component.type",
        "haystack.component.visits",
        "haystack.pipeline.max_runs_per_component",
        "haystack.pipeline.execution_mode",
        "haystack.agent.max_steps",
        "haystack.agent.steps_taken",
        "haystack.agent.step",
        "haystack.tool.name",
        "haystack.tool.error",
    }
)
_INPUT_TAGS = frozenset(
    {
        "haystack.pipeline.input_data",
        "haystack.component.input",
        "haystack.agent.input",
        "haystack.agent.step.llm.input",
        "haystack.agent.step.tool.input",
    }
)
_OUTPUT_TAGS = frozenset(
    {
        "haystack.pipeline.output_data",
        "haystack.component.output",
        "haystack.component.outputs",
        "haystack.agent.output",
        "haystack.agent.step.llm.output",
        "haystack.agent.step.tool.output",
    }
)


class HaystackSpan(Span):
    def __init__(
        self,
        raw: trace_api.Span,
        operation: str,
        config: TraceConfig,
        tags: Mapping[str, Any],
        component: Any = None,
    ) -> None:
        self._raw = raw
        self.operation = operation
        self.config = config
        self.component = component
        self.arguments: Mapping[str, Any] = {}
        self.pipeline_output: Any = None
        self.deferred: list[tuple["HaystackSpan", int]] = []
        self.finished = False
        self.component_type = sem.ComponentType.UNKNOWN
        self.kind = "CHAIN"
        if component is not None:
            try:
                self.component_type = sem._get_component_type(component)
            except Exception as exc:
                _log_extraction_failure("Unable to classify Haystack component", exc)
        else:
            name = str(tags.get("haystack.component.type", ""))
            for fragment, category in (
                ("Generator", "GENERATOR"),
                ("Embedder", "EMBEDDER"),
                ("Ranker", "RANKER"),
                ("Retriever", "RETRIEVER"),
                ("PromptBuilder", "PROMPT_BUILDER"),
            ):
                if fragment in name:
                    self.component_type = sem.ComponentType[category]
                    break
        self.kind = {
            sem.ComponentType.GENERATOR: "LLM",
            sem.ComponentType.EMBEDDER: "EMBEDDING",
            sem.ComponentType.RANKER: "RERANKER",
            sem.ComponentType.RETRIEVER: "RETRIEVER",
            sem.ComponentType.PROMPT_BUILDER: "LLM",
        }.get(self.component_type, "CHAIN")
        if operation == "haystack.agent.run":
            self.kind = "AGENT"
        elif operation == "haystack.agent.step.llm":
            self.kind = "LLM"
        elif operation == "haystack.agent.step.tool":
            self.kind = "TOOL"
        self._raw.set_attribute(sem.GEN_AI_SPAN_KIND, self.kind)
        self.set_tags(dict(tags))
        self._extract_static_metadata()

    def _attributes(self, attributes: Iterator[tuple[str, Any]]) -> None:
        # A malformed optional model response must not change application results.
        try:
            for key, value in attributes:
                if value is not None:
                    # These legacy semantic keys are not all directional in
                    # FITracer. Do not let template/embedding aliases bypass
                    # the same controls applied to input.value and messages.
                    private_input = any(
                        (
                            self.config.hide_inputs,
                            self.config.hide_input_messages,
                            self.config.hide_input_text,
                            self.config.hide_input_images,
                        )
                    )
                    private_output = any(
                        (
                            self.config.hide_outputs,
                            self.config.hide_output_messages,
                            self.config.hide_output_text,
                        )
                    )
                    if private_input and (
                        key.startswith("gen_ai.prompt.template.")
                        or key.endswith(".embedding.text")
                        or key.startswith(
                            ("reranker.query", "reranker.input_documents")
                        )
                    ):
                        value = "__REDACTED__"
                    elif private_output and key.startswith(
                        ("retrieval.documents", "reranker.output_documents")
                    ):
                        value = "__REDACTED__"
                    self._raw.set_attribute(key, value)
        except Exception as exc:
            _log_extraction_failure("Unable to extract Haystack span attributes", exc)

    def _extract_static_metadata(self) -> None:
        if self.component_type is sem.ComponentType.EMBEDDER:
            self._attributes(sem._get_embedding_model_attributes(self.component))
        elif self.component_type is sem.ComponentType.RANKER:
            self._attributes(sem._get_reranker_model_attributes(self.component))

    def set_tag(self, key: str, value: Any) -> None:
        if key in _INPUT_TAGS:
            self._input(value)
        elif key in _OUTPUT_TAGS:
            if key == "haystack.pipeline.output_data":
                # Haystack 2.x supplies a mutable output dict at span creation.
                # Serialize it on exit, once execution has populated it.
                self.pipeline_output = value
            else:
                self._output(value)
        elif key in _SAFE_TAGS and isinstance(value, (str, bool, int, float)):
            self._raw.set_attribute(key, value)
            if key == "haystack.tool.name":
                self._raw.set_attribute("gen_ai.tool.name", value)
            elif key == "haystack.tool.error" and value:
                self._raw.set_status(trace_api.StatusCode.ERROR)

    def set_content_tag(self, key: str, value: Any) -> None:
        # Apply the instrumentor's effective TraceConfig, not global toggles in a
        # second tracing backend. Non-content model/token fields survive opt-out.
        self.set_tag(key, value)

    def _input(self, value: Any) -> None:
        arguments = value if isinstance(value, Mapping) else {"input": value}
        self.arguments = arguments
        if (
            self.config.hide_inputs
            or self.config.hide_input_messages
            or self.config.hide_input_text
            or self.config.hide_input_images
        ):
            self._raw.set_attribute(sem.INPUT_VALUE, "__REDACTED__")
        else:
            self._attributes(sem._get_input_attributes(arguments))
        if self.kind == "LLM":
            self._attributes(sem._get_llm_input_message_attributes(arguments))
        if self.component_type is sem.ComponentType.RANKER:
            self._attributes(sem._get_reranker_request_attributes(arguments))
        if (
            self.component_type is sem.ComponentType.PROMPT_BUILDER
            and self.component is not None
        ):
            try:
                bound = sem._get_bound_arguments(self.component.run, **arguments)
                self._attributes(
                    sem._get_llm_prompt_template_attributes_from_prompt_builder(
                        self.component, bound
                    )
                )
            except Exception as exc:
                _log_extraction_failure(
                    "Unable to extract Haystack prompt template", exc
                )

    def _output(self, value: Any) -> None:
        response = value if isinstance(value, Mapping) else {"output": value}
        if (
            self.config.hide_outputs
            or self.config.hide_output_messages
            or self.config.hide_output_text
        ):
            self._raw.set_attribute(sem.OUTPUT_VALUE, "__REDACTED__")
        else:
            self._attributes(
                sem._get_component_output_attributes(response, self.component_type)
            )
        if self.kind == "LLM":
            self._attributes(sem._get_llm_model_attributes(response))
            self._attributes(sem._get_llm_token_count_attributes(response))
            self._attributes(sem._get_llm_output_message_attributes(response))
        elif self.component_type is sem.ComponentType.EMBEDDER:
            self._attributes(sem._get_embedding_attributes(self.arguments, response))
        elif self.component_type is sem.ComponentType.RANKER:
            self._attributes(sem._get_reranker_response_attributes(response))
        elif self.component_type is sem.ComponentType.RETRIEVER:
            self._attributes(sem._get_retriever_response_attributes(response))

    def finish(self, end_time: Optional[int] = None) -> None:
        if self.finished:
            return
        self.finished = True
        if self.pipeline_output is not None:
            self._output(self.pipeline_output)
        for child, child_end_time in self.deferred:
            child.finish(child_end_time)
        self.deferred.clear()
        self.arguments = {}
        self.pipeline_output = None
        self.component = None
        self._raw.end(end_time=end_time)

    def raw_span(self) -> trace_api.Span:
        return self._raw

    def get_correlation_data_for_logs(self) -> dict[str, Any]:
        context = self._raw.get_span_context()
        return {"trace_id": context.trace_id, "span_id": context.span_id}


class HaystackTracer(Tracer):
    def __init__(self, tracer: FITracer, config: TraceConfig) -> None:
        self._tracer = tracer
        self._config = config

    @contextmanager
    def trace(
        self,
        operation_name: str,
        tags: Optional[dict[str, Any]] = None,
        parent_span: Optional[Span] = None,
    ) -> Iterator[Span]:
        tags = tags or {}
        parent = parent_span if parent_span is not None else self.current_span()
        context = None
        if parent is not None and isinstance(parent.raw_span(), trace_api.Span):
            context = trace_api.set_span_in_context(parent.raw_span())
        component = None
        pipeline = pipeline_context.get()
        if pipeline is not None and operation_name == "haystack.component.run":
            try:
                component = pipeline.get_component(tags.get("haystack.component.name"))
            except Exception as exc:
                _log_extraction_failure(
                    "Unable to resolve Haystack component metadata", exc
                )
        # Preserve established pipeline/component names. New native operations
        # (agents/tools) retain their stable Haystack names.
        if operation_name in ("haystack.pipeline.run", "haystack.async_pipeline.run"):
            name = "Pipeline.run"
        elif operation_name == "haystack.component.run":
            name = f"{tags.get('haystack.component.type', 'Component')}.run"
        else:
            name = operation_name
        raw = self._tracer.start_span(name, context=context)
        span = HaystackSpan(raw, operation_name, self._config, tags, component)
        token = _active_span.set(span)
        try:
            with trace_api.use_span(raw, end_on_exit=False):
                yield span
        finally:
            _active_span.reset(token)
            # 2.10's AsyncPipeline sends outputs *after* its component context
            # exits. Keep it writable until the pipeline ends, preserving its
            # execution end timestamp rather than inflating component latency.
            if (
                isinstance(parent, HaystackSpan)
                and parent.operation == "haystack.async_pipeline.run"
                and not parent.finished
            ):
                parent.deferred.append((span, time_ns()))
            else:
                span.finish()

    def current_span(self) -> Optional[Span]:
        active = _active_span.get()
        raw = trace_api.get_current_span()
        # Prefer an intervening provider/user span over the enclosing Haystack
        # span, so a nested pipeline keeps its natural OTel parent.
        if active is not None and active.raw_span() is raw:
            return active
        if raw.get_span_context().is_valid:
            return _ExternalSpan(raw)
        return None


class _ExternalSpan(Span):
    def __init__(self, raw: trace_api.Span) -> None:
        self._raw = raw

    def raw_span(self) -> trace_api.Span:
        return self._raw

    def set_tag(self, key: str, value: Any) -> None:
        # Never attach unfiltered framework content to a foreign span.
        pass
