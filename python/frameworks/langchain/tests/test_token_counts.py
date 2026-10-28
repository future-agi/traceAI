"""Token counts on chat model spans come from LangChain's ``usage_metadata``.

``llm_output["usage"]`` is the provider's raw usage. Anthropic's raw
``input_tokens`` leaves out cached prompt tokens (``cache_read_input_tokens``,
``cache_creation_input_tokens``), while LangChain's ``usage_metadata`` counts
them, as the GenAI semantic conventions do. Deep Agents turns on Anthropic
prompt caching by default, so with the raw numbers a real Deep Agents run
reported 4 input tokens for a 4018-token prompt.
"""

from __future__ import annotations

import contextlib
from typing import Any, List, Optional

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class _CachedPromptModel(BaseChatModel):
    """Answers like ChatAnthropic does for a prompt-cache write."""

    raw_usage: dict = {}
    usage_metadata: Optional[dict] = None

    @property
    def _llm_type(self) -> str:
        return "cached-prompt-fake"

    def _generate(self, messages: List[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any):
        message = AIMessage(content="ok", usage_metadata=self.usage_metadata)
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output={"usage": self.raw_usage})


@pytest.fixture
def exporter():
    from traceai_langchain import LangChainInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter
    finally:
        with contextlib.suppress(Exception):
            instrumentor.uninstrument()


def _usage(exporter):
    (span,) = exporter.get_finished_spans()
    attrs = span.attributes or {}
    return tuple(attrs.get(f"gen_ai.usage.{k}") for k in ("input_tokens", "output_tokens", "total_tokens"))


def test_cached_prompt_tokens_are_counted(exporter):
    model = _CachedPromptModel(
        raw_usage={"input_tokens": 4, "output_tokens": 139, "cache_creation_input_tokens": 4014},
        usage_metadata={
            "input_tokens": 4018,
            "output_tokens": 139,
            "total_tokens": 4157,
            "input_token_details": {"cache_creation": 4014, "cache_read": 0},
        },
    )
    model.invoke("hi")
    assert _usage(exporter) == (4018, 139, 4157)


def test_provider_usage_is_used_without_usage_metadata(exporter):
    model = _CachedPromptModel(raw_usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
    model.invoke("hi")
    assert _usage(exporter) == (7, 3, 10)
