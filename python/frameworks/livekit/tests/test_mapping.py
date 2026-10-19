"""
Tests for the LiveKit -> Future AGI attribute mapping, on plain attribute dicts.

LiveKit 1.7 moved its conversation content under `lk.pii.` (`lk.user_input` became
`lk.pii.user_input`, and so on; 1.8 keeps them). The mapping has to read both those names and the earlier ones,
and a mapping failure has to be visible instead of passing silently. No LiveKit runtime is needed.
"""

import json
import logging
from typing import Any, Dict, List

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from traceai_livekit.exporters import base_exporter
from traceai_livekit.exporters.base_exporter import (
    BaseMappedSpanExporter,
    _map_attributes_to_fi_conventions,
)

NAMES_1_8 = {
    "user_input": "lk.pii.user_input",
    "instructions": "lk.pii.instructions",
    "chat_ctx": "lk.pii.chat_ctx",
    "response_text": "lk.pii.response.text",
    "function_calls": "lk.pii.response.function_calls",
    "tool_args": "lk.pii.function_tool.arguments",
    "tool_output": "lk.pii.function_tool.output",
    "tts_input": "lk.pii.input_text",
    "user_transcript": "lk.pii.user_transcript",
    "room_name": "lk.pii.room_name",
    "participant_identity": "lk.pii.participant_identity",
}
NAMES_PRE_1_8 = {
    "user_input": "lk.user_input",
    "instructions": "lk.instructions",
    "chat_ctx": "lk.chat_ctx",
    "response_text": "lk.response.text",
    "function_calls": "lk.response.function_calls",
    "tool_args": "lk.function_tool.arguments",
    "tool_output": "lk.function_tool.output",
    "tts_input": "lk.input_text",
    "user_transcript": "lk.user_transcript",
    "room_name": "lk.room_name",
    "participant_identity": "lk.participant_identity",
}

CHAT_CTX = json.dumps(
    {
        "items": [
            {"type": "message", "role": "system", "content": ["You book restaurant tables."]},
            {"type": "message", "role": "user", "content": ["Book a table for two at 7pm"]},
        ]
    }
)
USER_TEXT = "Book a table for two at 7pm"
REPLY_TEXT = "Your table for two is booked at 7pm."


@pytest.fixture(params=["1.8", "pre-1.8"])
def names(request: pytest.FixtureRequest) -> Dict[str, str]:
    """The attribute names of one LiveKit generation."""
    return NAMES_1_8 if request.param == "1.8" else NAMES_PRE_1_8


def test_an_llm_node_span_gets_its_chat_context_and_reply(names: Dict[str, str]) -> None:
    mapped = _map_attributes_to_fi_conventions(
        {
            names["chat_ctx"]: CHAT_CTX,
            names["response_text"]: REPLY_TEXT,
            "gen_ai.request.model": "probe-model-1",
        }
    )

    assert mapped[SpanAttributes.INPUT_VALUE] == CHAT_CTX
    assert json.loads(mapped[SpanAttributes.OUTPUT_VALUE]) == REPLY_TEXT
    assert mapped[SpanAttributes.GEN_AI_SPAN_KIND] == "LLM"
    assert mapped[SpanAttributes.GEN_AI_REQUEST_MODEL] == "probe-model-1"


@pytest.mark.parametrize("no_calls", ["[]", "", [], None])
def test_an_llm_node_with_no_function_calls_keeps_the_reply_alone_as_output(
    names: Dict[str, str], no_calls: Any
) -> None:
    # LiveKit 1.8 sets the function calls on every LLM node, as "[]" when there were none.
    mapped = _map_attributes_to_fi_conventions(
        {
            names["chat_ctx"]: CHAT_CTX,
            names["response_text"]: REPLY_TEXT,
            names["function_calls"]: no_calls,
        }
    )

    assert json.loads(mapped[SpanAttributes.OUTPUT_VALUE]) == REPLY_TEXT


def test_an_llm_node_with_function_calls_keeps_them_next_to_the_reply(
    names: Dict[str, str],
) -> None:
    calls = json.dumps([{"name": "book_table", "arguments": "{\"party\": 2}"}])
    mapped = _map_attributes_to_fi_conventions(
        {names["response_text"]: REPLY_TEXT, names["function_calls"]: calls}
    )

    assert json.loads(mapped[SpanAttributes.OUTPUT_VALUE]) == {
        "text": REPLY_TEXT,
        "function_calls": calls,
    }


def test_an_agent_turn_span_gets_the_user_input_and_reply(names: Dict[str, str]) -> None:
    mapped = _map_attributes_to_fi_conventions(
        {names["user_input"]: USER_TEXT, names["response_text"]: REPLY_TEXT}
    )

    assert json.loads(mapped[SpanAttributes.INPUT_VALUE]) == USER_TEXT
    assert json.loads(mapped[SpanAttributes.OUTPUT_VALUE]) == REPLY_TEXT
    assert mapped[SpanAttributes.GEN_AI_SPAN_KIND] == "CHAIN"


def test_a_tool_span_gets_its_arguments_and_output(names: Dict[str, str]) -> None:
    arguments = json.dumps({"party_size": 2, "time": "19:00"})

    mapped = _map_attributes_to_fi_conventions(
        {
            "lk.function_tool.name": "book_table",
            names["tool_args"]: arguments,
            names["tool_output"]: "booked",
        }
    )

    assert mapped[SpanAttributes.INPUT_VALUE] == arguments
    assert json.loads(mapped[SpanAttributes.OUTPUT_VALUE]) == "booked"
    assert mapped[SpanAttributes.GEN_AI_SPAN_KIND] == "TOOL"
    assert mapped[SpanAttributes.GEN_AI_TOOL_NAME] == "book_table"


def test_a_tts_span_gets_the_text_to_speak(names: Dict[str, str]) -> None:
    mapped = _map_attributes_to_fi_conventions({names["tts_input"]: REPLY_TEXT})

    assert json.loads(mapped[SpanAttributes.INPUT_VALUE]) == REPLY_TEXT
    assert mapped[SpanAttributes.GEN_AI_SPAN_KIND] == "LLM"


def test_a_session_span_gets_its_conversation_and_user(names: Dict[str, str]) -> None:
    mapped = _map_attributes_to_fi_conventions(
        {
            names["room_name"]: "room-42",
            names["participant_identity"]: "caller-7",
        }
    )

    assert mapped[SpanAttributes.GEN_AI_CONVERSATION_ID] == "room-42"
    assert mapped[SpanAttributes.USER_ID] == "caller-7"


def test_instructions_and_the_user_transcript_go_to_the_metadata(names: Dict[str, str]) -> None:
    mapped = _map_attributes_to_fi_conventions(
        {
            names["instructions"]: "You book restaurant tables.",
            names["user_transcript"]: USER_TEXT,
        }
    )

    metadata = json.loads(mapped[SpanAttributes.METADATA])
    assert metadata[names["instructions"]] == "You book restaurant tables."
    assert metadata[names["user_transcript"]] == USER_TEXT


def test_the_1_8_name_wins_when_both_names_are_present() -> None:
    mapped = _map_attributes_to_fi_conventions(
        {
            "lk.pii.chat_ctx": json.dumps({"from": "1.8"}),
            "lk.chat_ctx": json.dumps({"from": "pre-1.8"}),
            "lk.pii.response.text": "from 1.8",
            "lk.response.text": "from pre-1.8",
            "lk.pii.room_name": "room-1-8",
            "lk.room_name": "room-pre-1-8",
            "lk.pii.participant_identity": "identity-1-8",
            "lk.participant_identity": "identity-pre-1-8",
        }
    )

    assert json.loads(mapped[SpanAttributes.INPUT_VALUE]) == {"from": "1.8"}
    assert json.loads(mapped[SpanAttributes.OUTPUT_VALUE]) == "from 1.8"
    assert mapped[SpanAttributes.GEN_AI_CONVERSATION_ID] == "room-1-8"
    assert mapped[SpanAttributes.USER_ID] == "identity-1-8"


def test_a_mapping_failure_is_logged_once_and_the_attributes_are_kept(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # fi-instrumentation-otel before 1.0.0 has no GEN_AI_SPAN_KIND, so the mapping raises.
    monkeypatch.delattr(base_exporter.SpanAttributes, "GEN_AI_SPAN_KIND")
    monkeypatch.setattr(base_exporter, "_mapping_failure_logged", False)
    secret = "what-the-caller-said"
    attributes: Dict[str, Any] = {"lk.pii.user_input": secret}
    exporter = BaseMappedSpanExporter()

    with caplog.at_level(logging.WARNING, logger=base_exporter.__name__):
        first = exporter._convert_attributes(attributes)
        second = exporter._convert_attributes(attributes)

    assert first == attributes
    assert second == attributes
    warnings = [
        r
        for r in caplog.records
        if r.name == base_exporter.__name__ and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "AttributeError" in message
    assert "fi-instrumentation-otel >= 1.1.0" in message
    assert secret not in message


class _Recorder(SpanExporter):
    """Stands in for the OTLP exporter under the mapping: records what it is handed."""

    def __init__(self) -> None:
        self.exported: List[Any] = []

    def export(self, spans: Any) -> SpanExportResult:
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


class _MappedRecorder(BaseMappedSpanExporter, _Recorder):
    pass


class _UnreadableAttributes:
    def keys(self) -> Any:
        raise RuntimeError("cannot read the attributes")


class _Span:
    def __init__(self, attributes: Any) -> None:
        self.name = "span"
        self._attributes = attributes


def test_a_span_that_cannot_be_mapped_is_still_exported_and_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(base_exporter, "_mapping_failure_logged", False)
    unreadable = [_Span(_UnreadableAttributes()), _Span(_UnreadableAttributes())]
    readable = _Span({"lk.pii.user_input": USER_TEXT})
    exporter = _MappedRecorder()

    with caplog.at_level(logging.WARNING, logger=base_exporter.__name__):
        result = exporter.export([*unreadable, readable])

    assert result is SpanExportResult.SUCCESS
    assert exporter.exported == [*unreadable, readable]
    assert json.loads(readable._attributes[SpanAttributes.INPUT_VALUE]) == USER_TEXT
    warnings = [
        r
        for r in caplog.records
        if r.name == base_exporter.__name__ and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()


def test_a_span_is_exported_with_its_own_attributes_when_the_mapping_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(base_exporter.SpanAttributes, "GEN_AI_SPAN_KIND")
    monkeypatch.setattr(base_exporter, "_mapping_failure_logged", False)
    attributes = {"lk.pii.user_input": USER_TEXT}
    span = _Span(dict(attributes))
    exporter = _MappedRecorder()

    assert exporter.export([span]) is SpanExportResult.SUCCESS

    assert exporter.exported == [span]
    assert span._attributes == attributes
