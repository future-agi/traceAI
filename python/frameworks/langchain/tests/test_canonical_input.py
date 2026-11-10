"""canonical input must retain the full chat conversation."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fi_instrumentation import REDACTED_VALUE, TraceConfig
from fi_instrumentation.fi_types import MessageAttributes, SpanAttributes
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def exporter(request):
    from traceai_langchain import LangChainInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(**getattr(request, "param", {}))
    )
    try:
        yield exporter
    finally:
        try:
            instrumentor.uninstrument()
        finally:
            provider.shutdown()


@pytest.mark.parametrize(
    "messages",
    [
        pytest.param(
            [HumanMessage(content="CURRENT_USER_QUESTION_MARKER")],
            id="single-user",
        ),
        pytest.param(
            [
                SystemMessage(content="SYSTEM_INSTRUCTION_MARKER"),
                HumanMessage(content="CURRENT_USER_QUESTION_MARKER"),
            ],
            id="system-first",
        ),
        pytest.param(
            [
                HumanMessage(content="PREVIOUS_USER_QUESTION_MARKER"),
                AIMessage(content="PREVIOUS_ASSISTANT_ANSWER_MARKER"),
                HumanMessage(content="CURRENT_USER_QUESTION_MARKER"),
            ],
            id="history-first",
        ),
        pytest.param(
            [
                SystemMessage(content="SYSTEM_INSTRUCTION_MARKER"),
                HumanMessage(content="PREVIOUS_USER_QUESTION_MARKER"),
                AIMessage(content="PREVIOUS_ASSISTANT_ANSWER_MARKER"),
                HumanMessage(content="CURRENT_USER_QUESTION_MARKER"),
            ],
            id="system-and-history",
        ),
    ],
)
def test_canonical_input_preserves_conversation(exporter, messages):
    original_messages = deepcopy(messages)
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="offline answer")], cache=False
    )

    response = model.invoke(messages)

    assert response.content == "offline answer"
    assert messages == original_messages
    (span,) = [
        span
        for span in exporter.get_finished_spans()
        if (span.attributes or {}).get(SpanAttributes.GEN_AI_SPAN_KIND) == "LLM"
    ]
    attrs = span.attributes or {}
    canonical_input = json.loads(attrs[SpanAttributes.INPUT_VALUE])
    # At the audited revision this is only the first message's content, even
    # though structured message attributes still contain the current question.
    assert isinstance(canonical_input, dict), "canonical input lost the conversation"
    (conversation,) = canonical_input["messages"]
    assert [
        (message["id"][-1], message["kwargs"]["content"])
        for message in conversation
    ] == [(type(message).__name__, message.content) for message in original_messages]

    last_message_content = (
        f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{len(messages) - 1}."
        f"{MessageAttributes.MESSAGE_CONTENT}"
    )
    assert attrs[last_message_content] == "CURRENT_USER_QUESTION_MARKER"


_IMAGE_PAYLOAD = "c3ludGhldGljLWltYWdlLXBheWxvYWQ="
_IMAGE_URL = "https://example.invalid/picture.png"
_IMAGE_DATA_URL = f"data:image/png;base64,{_IMAGE_PAYLOAD}"


@pytest.mark.parametrize(
    "image_url, expected_url",
    [
        pytest.param(_IMAGE_URL, _IMAGE_URL, id="string-url"),
        pytest.param(_IMAGE_DATA_URL, REDACTED_VALUE, id="string-data-url"),
        pytest.param({"url": _IMAGE_URL}, _IMAGE_URL, id="dict-url"),
        pytest.param({"url": _IMAGE_DATA_URL}, REDACTED_VALUE, id="dict-data-url"),
    ],
)
@pytest.mark.parametrize(
    "exporter, hide_inputs",
    [
        pytest.param({"hide_inputs": False}, False, id="visible"),
        pytest.param({"hide_inputs": True}, True, id="hidden"),
    ],
    indirect=["exporter"],
)
def test_canonical_input_preserves_image_messages(
    exporter, hide_inputs, image_url, expected_url, caplog
):
    text = "Describe this synthetic picture"
    messages = [
        HumanMessage(
            content=[
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": image_url},
            ]
        )
    ]
    original_messages = deepcopy(messages)
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="offline answer")], cache=False
    )

    response = model.invoke(messages)

    assert response.content == "offline answer"
    assert messages == original_messages
    (span,) = [
        span
        for span in exporter.get_finished_spans()
        if (span.attributes or {}).get(SpanAttributes.GEN_AI_SPAN_KIND) == "LLM"
    ]
    assert span.status.status_code.name == "OK"
    attrs = span.attributes or {}
    assert SpanAttributes.INPUT_VALUE in attrs, "canonical input is missing"
    exported = json.dumps([dict(s.attributes or {}) for s in exporter.get_finished_spans()])
    assert _IMAGE_PAYLOAD not in exported
    assert _IMAGE_DATA_URL not in exported
    assert not [
        record
        for record in caplog.records
        if record.name.startswith("traceai_langchain") and record.levelno >= 40
    ]
    if hide_inputs:
        assert attrs[SpanAttributes.INPUT_VALUE] == REDACTED_VALUE
        assert text not in exported
        assert _IMAGE_URL not in exported
    else:
        canonical_input = json.loads(attrs[SpanAttributes.INPUT_VALUE])
        (conversation,) = canonical_input["messages"]
        (message,) = conversation
        assert message["id"][-1] == "HumanMessage"
        assert message["kwargs"]["content"] == [
            {"type": "text", "text": text},
            {"type": "text", "text": f"[Image: {expected_url}]"},
        ]
        assert json.loads(attrs[SpanAttributes.INPUT_IMAGES]) == [expected_url]
        assert expected_url in exported
