import asyncio
import json
import logging
from importlib.metadata import version

import pytest
from fi_instrumentation.fi_types import FiSpanKindValues, MessageAttributes, SpanAttributes
from llama_index.core import Document, VectorStoreIndex
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.instrumentation import get_dispatcher
from llama_index.core.instrumentation.events import BaseEvent
from llama_index.core.llms import ChatMessage
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai import OpenAI
from openai import BadRequestError
from opentelemetry.trace import StatusCode

from .conftest import (
    ANSWER,
    FAILING_MODEL,
    FAILING_MODEL_ERROR,
    TOOL_ARGUMENTS,
    USAGE,
    wait_for_span,
)

OUTPUT_MESSAGE_CONTENT = (
    f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"
)
QUESTION = "What does FutureAGI build?"

dispatcher = get_dispatcher(__name__)


class ProgressEvent(BaseEvent):
    @classmethod
    def class_name(cls) -> str:
        return "ProgressEvent"


@dispatcher.span
def report_progress(steps: int) -> int:
    for _ in range(steps):
        dispatcher.event(ProgressEvent())
    return steps


def _llm(base_url: str, model: str = "gpt-4o") -> OpenAI:
    return OpenAI(model=model, api_base=base_url, api_key="sk-test", max_retries=0)


def _index() -> VectorStoreIndex:
    documents = [Document(text="FutureAGI builds evaluation tools for AI agents.")]
    return VectorStoreIndex.from_documents(documents, embed_model=MockEmbedding(embed_dim=8))


def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


def _run_agent(agent: FunctionAgent) -> str:
    async def run() -> str:
        return str(await agent.run(user_msg="What is 2 times 3?"))

    return asyncio.run(run())


def test_instrumented_chat_emits_llm_span(exporter, openai_base_url):
    response = _llm(openai_base_url).chat([ChatMessage(role="user", content=QUESTION)])

    assert response.message.content == ANSWER
    span = wait_for_span(exporter, "OpenAI.chat")
    assert span.status.status_code is StatusCode.OK
    assert span.attributes[SpanAttributes.GEN_AI_SPAN_KIND] == FiSpanKindValues.LLM.value
    assert span.attributes[OUTPUT_MESSAGE_CONTENT] == ANSWER
    assert span.attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == USAGE["total_tokens"]


def test_streaming_query_reaches_caller_intact(exporter, openai_base_url):
    engine = _index().as_query_engine(llm=_llm(openai_base_url), streaming=True)

    streamed = "".join(engine.query(QUESTION).response_gen)

    assert streamed == ANSWER
    llm_span = wait_for_span(exporter, "OpenAI.stream_chat")
    assert llm_span.attributes[OUTPUT_MESSAGE_CONTENT] == ANSWER
    query_span = wait_for_span(exporter, "RetrieverQueryEngine.query")
    assert query_span.status.status_code is StatusCode.OK


def test_streaming_chat_reaches_caller_intact(exporter, openai_base_url):
    engine = _index().as_chat_engine(chat_mode="context", llm=_llm(openai_base_url))

    streamed = "".join(engine.stream_chat(QUESTION).response_gen)

    assert streamed == ANSWER
    span = wait_for_span(exporter, "OpenAI.stream_chat")
    assert span.attributes[OUTPUT_MESSAGE_CONTENT] == ANSWER


def test_async_streaming_chat_reaches_caller_intact(exporter, openai_base_url):
    engine = _index().as_chat_engine(chat_mode="context", llm=_llm(openai_base_url))

    async def consume() -> str:
        response = await engine.astream_chat(QUESTION)
        return "".join([token async for token in response.async_response_gen()])

    assert asyncio.run(consume()) == ANSWER
    span = wait_for_span(exporter, "OpenAI.astream_chat")
    assert span.attributes[OUTPUT_MESSAGE_CONTENT] == ANSWER


def test_function_agent_run_is_traced_as_agent(exporter, openai_base_url):
    agent = FunctionAgent(
        tools=[FunctionTool.from_defaults(fn=multiply)],
        llm=_llm(openai_base_url),
    )

    assert _run_agent(agent) == ANSWER
    agent_span = wait_for_span(exporter, "FunctionAgent.run")
    assert agent_span.status.status_code is StatusCode.OK
    assert agent_span.attributes[SpanAttributes.GEN_AI_SPAN_KIND] == FiSpanKindValues.AGENT.value
    assert ANSWER in agent_span.attributes[SpanAttributes.OUTPUT_VALUE]
    tool_span = wait_for_span(exporter, "FunctionTool.acall")
    assert tool_span.attributes[SpanAttributes.GEN_AI_SPAN_KIND] == FiSpanKindValues.TOOL.value
    assert tool_span.attributes[SpanAttributes.GEN_AI_TOOL_NAME] == multiply.__name__
    tool_output = json.loads(tool_span.attributes[SpanAttributes.OUTPUT_VALUE])
    assert tool_output["raw_output"] == multiply(**TOOL_ARGUMENTS)


def test_llm_error_marks_span_and_propagates(exporter, openai_base_url):
    llm = _llm(openai_base_url, model=FAILING_MODEL)

    with pytest.raises(BadRequestError, match=FAILING_MODEL_ERROR):
        llm.chat([ChatMessage(role="user", content=QUESTION)])

    span = wait_for_span(exporter, "OpenAI.chat")
    assert span.status.status_code is StatusCode.ERROR
    assert FAILING_MODEL_ERROR in span.status.description
    assert span.attributes[SpanAttributes.GEN_AI_SPAN_KIND] == FiSpanKindValues.LLM.value
    assert [event.name for event in span.events] == ["exception"]


def test_unhandled_events_are_logged_once_per_type(exporter, caplog):
    with caplog.at_level(logging.WARNING, logger="traceai_llamaindex"):
        assert report_progress(steps=3) == 3
        assert report_progress(steps=2) == 2

    unhandled = [r.getMessage() for r in caplog.records if "Unhandled event" in r.getMessage()]
    assert unhandled == [f"Unhandled event of type {ProgressEvent.__qualname__}"]
    span = wait_for_span(exporter, report_progress.__qualname__)
    assert span.status.status_code is StatusCode.OK


def test_spans_report_installed_package_version(exporter, openai_base_url):
    _llm(openai_base_url).chat([ChatMessage(role="user", content=QUESTION)])

    span = wait_for_span(exporter, "OpenAI.chat")
    assert span.instrumentation_scope.version == version("traceAI-llamaindex")
