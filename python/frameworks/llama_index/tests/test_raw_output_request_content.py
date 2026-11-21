"""Streaming chat output must not carry request messages that input settings hide."""
import json

import pytest
from fi_instrumentation import TraceConfig
from llama_index.core.base.llms.types import (
    ChatMessage,
    CompletionResponse,
    CompletionResponseGen,
    LLMMetadata,
)
from llama_index.core.llms import CustomLLM
from llama_index.core.llms.callbacks import llm_completion_callback
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_llamaindex import LlamaIndexInstrumentor

MESSAGE_SENTINEL = "SENTINEL_CHAT_MESSAGE_8127"
REPLY = "synthetic generated reply"


class OfflineLLM(CustomLLM):
    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(model_name="offline")

    @llm_completion_callback()
    def complete(self, prompt: str, formatted: bool = False, **kwargs) -> CompletionResponse:
        return CompletionResponse(text=REPLY)

    @llm_completion_callback()
    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs) -> CompletionResponseGen:
        def gen():
            text = ""
            for word in REPLY.split():
                text += word + " "
                yield CompletionResponse(text=text, delta=word + " ")
        return gen()


def _run(config):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LlamaIndexInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        chunks = list(OfflineLLM().stream_chat([ChatMessage(role="user", content=MESSAGE_SENTINEL)]))
        assert chunks and REPLY.split()[0] in chunks[-1].message.content
        spans = exporter.get_finished_spans()
        assert spans
        return json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(
            [[(e.name, dict(e.attributes or {})) for e in s.events] for s in spans],
            default=str,
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings():
    assert REPLY.split()[0] in _run(TraceConfig())


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_streamOutputOmitsRequestMessagesUnderInputSettings(trace_config):
    assert MESSAGE_SENTINEL not in _run(TraceConfig(**trace_config))
