"""normalized tool calls must reach semantic attributes."""

from copy import deepcopy
import json

import pytest
from fi_instrumentation import TraceConfig
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_langchain import LangChainInstrumentor
from traceai_langchain._tracer import (
    _flatten,
    _input_messages,
    _output_messages,
    _parse_message_data,
)


def _serialized_message(kwargs, message_class="AIMessage"):
    return {
        "lc": 1,
        "type": "constructor",
        "id": ["langchain", "schema", "messages", message_class],
        "kwargs": {"content": "", **kwargs},
    }


def _normalized_calls():
    return [
        {"name": "lookup", "args": {"city": "Paris"}, "id": "call_test", "type": "tool_call"},
        {"name": "ping", "args": {}, "id": None, "type": "tool_call"},
    ]


def _provider_calls():
    return [{
        "id": "call_provider",
        "type": "function",
        "function": {"name": "provider_lookup", "arguments": '{"city": "Rome"}'},
    }]


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("message_class", ["AIMessage", "AIMessageChunk"])
def test_normalized_tool_calls_are_semantic_messages(direction, message_class):
    message = _serialized_message({"tool_calls": _normalized_calls()}, message_class)
    before = deepcopy(message)
    if direction == "input":
        attributes = dict(_flatten(_input_messages({"messages": [[message]]})))
    else:
        attributes = dict(_flatten(_output_messages({"generations": [[{"message": message}]]})))

    prefix = f"gen_ai.{direction}.messages.0"
    assert attributes == {
        f"{prefix}.message.role": "assistant",
        f"{prefix}.message.tool_calls.0.tool_call.id": "call_test",
        f"{prefix}.message.tool_calls.0.tool_call.function.name": "lookup",
        f"{prefix}.message.tool_calls.0.tool_call.function.arguments": json.dumps({"city": "Paris"}),
        f"{prefix}.message.tool_calls.1.tool_call.function.name": "ping",
        f"{prefix}.message.tool_calls.1.tool_call.function.arguments": "{}",
    }
    assert message == before


def test_normalized_tool_calls_take_precedence_without_duplicates():
    message = _serialized_message({
        "tool_calls": _normalized_calls()[:1],
        "additional_kwargs": {"tool_calls": [
            {"id": "call_test", "type": "function", "function": {
                "name": "lookup", "arguments": '{"city": "Paris"}',
            }},
            *_provider_calls(),
        ]},
    })
    before = deepcopy(message)
    attributes = dict(_parse_message_data(message))
    assert attributes["message.tool_calls"] == [{
        "tool_call.id": "call_test",
        "tool_call.function.name": "lookup",
        "tool_call.function.arguments": json.dumps({"city": "Paris"}),
    }]
    assert message == before


@pytest.mark.parametrize("normalized", [{}, {"tool_calls": []}], ids=["absent", "empty"])
def test_provider_tool_calls_remain_fallback(normalized):
    message = _serialized_message({
        **normalized,
        "additional_kwargs": {"tool_calls": _provider_calls()},
    })
    before = deepcopy(message)
    attributes = dict(_parse_message_data(message))
    assert attributes["message.tool_calls"] == [{
        "tool_call.id": "call_provider",
        "tool_call.function.name": "provider_lookup",
        "tool_call.function.arguments": '{"city": "Rome"}',
    }]
    assert message == before


def test_legacy_function_call_without_tool_calls_is_unchanged():
    message = _serialized_message({
        "content": "answer",
        "additional_kwargs": {"function_call": {
            "name": "lookup", "arguments": '{"city": "Paris"}',
        }},
    })
    assert dict(_parse_message_data(message)) == {
        "message.role": "assistant",
        "message.content": "answer",
        "message.function_call_name": "lookup",
        "message.function_call_arguments_json": '{"city": "Paris"}',
    }


def test_normalized_tool_calls_reach_exported_span():
    reply = AIMessage(content="", tool_calls=_normalized_calls()[:1])
    before = deepcopy(reply.tool_calls)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_outputs=False, hide_output_messages=False, hide_output_text=False),
    )
    try:
        result = FakeMessagesListChatModel(responses=[reply]).invoke("look up Paris")
        assert result.tool_calls == before
        assert result.additional_kwargs == {}
        spans = [
            span for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.span.kind") == "LLM"
        ]
        assert len(spans) == 1
        tool_attributes = {
            name: value for name, value in spans[0].attributes.items()
            if name.startswith("gen_ai.output.messages.0.message.tool_calls.")
        }
        prefix = "gen_ai.output.messages.0.message.tool_calls.0"
        assert tool_attributes == {
            f"{prefix}.tool_call.id": "call_test",
            f"{prefix}.tool_call.function.name": "lookup",
            f"{prefix}.tool_call.function.arguments": json.dumps({"city": "Paris"}),
        }
        assert reply.tool_calls == before
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
