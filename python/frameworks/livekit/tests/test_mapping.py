"""
Tests for the LiveKit -> Future AGI attribute mapping, on plain attribute dicts.

LiveKit 1.7 moved its conversation content under `lk.pii.` (`lk.user_input` became
`lk.pii.user_input`, and so on; 1.8 keeps them). The mapping has to read both those names and the earlier ones,
and a mapping failure has to be visible instead of passing silently. No LiveKit runtime is needed.
"""

import json
import logging
import random
from typing import Any, Dict, List

import pytest
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext, TraceFlags

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


def _json_text(value: str) -> Any:
    return json.loads(value)


# (key, 1.8 name, earlier name, which mapped attribute carries it, how to read it back).
# One case per renamed pair that reaches input.value, output.value or the session attributes: with
# both names present and different, swapping the lookup order makes exactly one of these fail.
_NAME_PAIRS = [
    pytest.param(
        "chat_ctx", SpanAttributes.INPUT_VALUE, _json_text, json.dumps({"from": "1.8"}),
        json.dumps({"from": "pre-1.8"}), {"from": "1.8"}, id="chat_ctx",
    ),
    pytest.param(
        "user_input", SpanAttributes.INPUT_VALUE, _json_text, "from 1.8", "from pre-1.8",
        "from 1.8", id="user_input",
    ),
    pytest.param(
        "response_text", SpanAttributes.OUTPUT_VALUE, _json_text, "from 1.8", "from pre-1.8",
        "from 1.8", id="response_text",
    ),
    pytest.param(
        "function_calls", SpanAttributes.OUTPUT_VALUE, _json_text,
        json.dumps([{"name": "from_1_8"}]), json.dumps([{"name": "from_pre_1_8"}]),
        [{"name": "from_1_8"}], id="function_calls",
    ),
    pytest.param(
        "tool_args", SpanAttributes.INPUT_VALUE, _json_text, json.dumps({"from": "1.8"}),
        json.dumps({"from": "pre-1.8"}), {"from": "1.8"}, id="tool_args",
    ),
    pytest.param(
        "tool_output", SpanAttributes.OUTPUT_VALUE, _json_text, "from 1.8", "from pre-1.8",
        "from 1.8", id="tool_output",
    ),
    pytest.param(
        "tts_input", SpanAttributes.INPUT_VALUE, _json_text, "from 1.8", "from pre-1.8",
        "from 1.8", id="tts_input",
    ),
    pytest.param(
        "room_name", SpanAttributes.GEN_AI_CONVERSATION_ID, lambda v: v, "room-1-8",
        "room-pre-1-8", "room-1-8", id="room_name",
    ),
    pytest.param(
        "participant_identity", SpanAttributes.USER_ID, lambda v: v, "identity-1-8",
        "identity-pre-1-8", "identity-1-8", id="participant_identity",
    ),
]


@pytest.mark.parametrize(
    "key, target, read, new_value, old_value, expected", _NAME_PAIRS
)
def test_the_1_8_name_wins_when_both_names_are_present(
    key: str, target: str, read: Any, new_value: str, old_value: str, expected: Any
) -> None:
    mapped = _map_attributes_to_fi_conventions(
        {NAMES_1_8[key]: new_value, NAMES_PRE_1_8[key]: old_value}
    )

    assert read(mapped[target]) == expected


@pytest.mark.parametrize(
    "key, target",
    [
        ("user_input", SpanAttributes.INPUT_VALUE),
        ("response_text", SpanAttributes.OUTPUT_VALUE),
        ("tts_input", SpanAttributes.INPUT_VALUE),
        ("tool_args", SpanAttributes.INPUT_VALUE),
        ("tool_output", SpanAttributes.OUTPUT_VALUE),
    ],
)
def test_an_empty_value_is_no_input_or_output(
    names: Dict[str, str], key: str, target: str
) -> None:
    mapped = _map_attributes_to_fi_conventions({names[key]: ""})

    # Not the JSON text '""'.
    assert target not in mapped


def _mapping_warnings(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == base_exporter.__name__ and r.levelno == logging.WARNING
    ]


def test_a_mapping_failure_is_logged_once_and_the_attributes_are_kept(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "what-the-caller-said"

    def failing(attributes: Any) -> Any:
        # A failure that carries the content: the log must not repeat it.
        raise ValueError(f"cannot map {secret}")

    monkeypatch.setattr(base_exporter, "_map_attributes_to_fi_conventions", failing)
    monkeypatch.setattr(base_exporter, "_mapping_failure_logged", False)
    attributes: Dict[str, Any] = {"lk.pii.user_input": secret}
    exporter = BaseMappedSpanExporter()

    with caplog.at_level(logging.WARNING, logger=base_exporter.__name__):
        first = exporter._convert_attributes(attributes)
        second = exporter._convert_attributes(attributes)

    assert first == attributes
    assert second == attributes
    warnings = _mapping_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "ValueError" in message
    assert "fi-instrumentation-otel >= 1.1.0" in message
    assert secret not in message
    assert secret not in caplog.text


def test_an_old_fi_instrumentation_without_the_span_kind_is_logged_and_the_attributes_are_kept(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # fi-instrumentation-otel before 1.0.0 has no GEN_AI_SPAN_KIND, so the mapping raises.
    monkeypatch.delattr(base_exporter.SpanAttributes, "GEN_AI_SPAN_KIND")
    monkeypatch.setattr(base_exporter, "_mapping_failure_logged", False)
    attributes: Dict[str, Any] = {"lk.pii.user_input": "what-the-caller-said"}
    exporter = BaseMappedSpanExporter()

    with caplog.at_level(logging.WARNING, logger=base_exporter.__name__):
        converted = exporter._convert_attributes(attributes)

    assert converted == attributes
    warnings = _mapping_warnings(caplog)
    assert len(warnings) == 1
    assert "AttributeError" in warnings[0].getMessage()
    assert "what-the-caller-said" not in warnings[0].getMessage()


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


def _context() -> SpanContext:
    return SpanContext(
        trace_id=random.getrandbits(128) or 1,
        span_id=random.getrandbits(64) or 1,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )


def _span(attributes: Dict[str, Any]) -> ReadableSpan:
    return ReadableSpan(name="span", context=_context(), attributes=attributes)


class _UnreadableSpan:
    """A span whose attributes cannot be read."""

    name = "unreadable"

    @property
    def attributes(self) -> Any:
        raise RuntimeError("cannot read the attributes")


def test_the_mapped_attributes_go_on_a_copy_and_the_span_handed_in_is_left_alone() -> None:
    original = {
        "lk.pii.user_input": USER_TEXT,
        "lk.pii.room_name": "room-42",
        "lk.pii.participant_identity": "caller-7",
    }
    span = _span(dict(original))
    exporter = _MappedRecorder()

    assert exporter.export([span]) is SpanExportResult.SUCCESS

    # The span other processors hold is exactly as it was: none of the mapped keys on it.
    assert dict(span.attributes) == original
    (exported,) = exporter.exported
    assert exported is not span
    assert json.loads(exported.attributes[SpanAttributes.INPUT_VALUE]) == USER_TEXT
    assert exported.attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == "room-42"
    assert exported.attributes[SpanAttributes.USER_ID] == "caller-7"
    # The copy is the same span otherwise.
    assert exported.name == span.name
    assert exported.context == span.context
    assert exported.parent == span.parent
    assert exported.start_time == span.start_time
    assert exported.end_time == span.end_time
    assert exported.status == span.status


def test_the_copy_keeps_every_other_field_of_the_span() -> None:
    # The copy replaces only the attributes: losing the resource would drop the project name
    # register() sets, losing the parent would break the trace tree.
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import Link, SpanKind, Status, StatusCode

    parent = _context()
    span = ReadableSpan(
        name="llm_node",
        context=_context(),
        parent=parent,
        resource=Resource.create({"project_name": "voice-agent"}),
        attributes={"lk.pii.user_input": USER_TEXT},
        events=[Event("first_token", {"n": 1}, timestamp=5)],
        links=[Link(_context(), {"why": "follows"})],
        kind=SpanKind.CLIENT,
        status=Status(StatusCode.ERROR, "model timed out"),
        start_time=1,
        end_time=9,
        instrumentation_scope=InstrumentationScope("livekit-agents", "1.8"),
    )
    exporter = _MappedRecorder()

    exporter.export([span])

    (exported,) = exporter.exported
    assert exported is not span
    assert exported.name == "llm_node"
    assert exported.context == span.context
    assert exported.parent == parent
    assert exported.resource.attributes["project_name"] == "voice-agent"
    assert [e.name for e in exported.events] == ["first_token"]
    assert [link.context for link in exported.links] == [span.links[0].context]
    assert exported.kind is SpanKind.CLIENT
    assert exported.status.status_code is StatusCode.ERROR
    assert exported.status.description == "model timed out"
    assert (exported.start_time, exported.end_time) == (1, 9)
    assert exported.instrumentation_scope == span.instrumentation_scope


def test_a_span_with_nothing_to_map_is_passed_on_as_it_is() -> None:
    span = _span({})
    exporter = _MappedRecorder()

    exporter.export([span])

    assert exporter.exported == [span]
    assert exporter.exported[0] is span


def test_a_span_that_cannot_be_read_is_exported_as_it_is_and_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(base_exporter, "_mapping_failure_logged", False)
    unreadable = [_UnreadableSpan(), _UnreadableSpan()]
    readable = _span({"lk.pii.user_input": USER_TEXT})
    exporter = _MappedRecorder()

    with caplog.at_level(logging.WARNING, logger=base_exporter.__name__):
        result = exporter.export([*unreadable, readable])

    assert result is SpanExportResult.SUCCESS
    # The unreadable spans are passed through as the same objects; none is dropped.
    assert len(exporter.exported) == 3
    assert exporter.exported[0] is unreadable[0]
    assert exporter.exported[1] is unreadable[1]
    assert json.loads(exporter.exported[2].attributes[SpanAttributes.INPUT_VALUE]) == USER_TEXT
    assert SpanAttributes.INPUT_VALUE not in readable.attributes
    warnings = _mapping_warnings(caplog)
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()


def test_a_span_is_exported_as_it_is_when_the_mapping_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "what-the-caller-said"

    def failing(attributes: Any) -> Any:
        raise ValueError(f"cannot map {secret}")

    monkeypatch.setattr(base_exporter, "_map_attributes_to_fi_conventions", failing)
    monkeypatch.setattr(base_exporter, "_mapping_failure_logged", False)
    attributes = {"lk.pii.user_input": secret}
    span = _span(dict(attributes))
    exporter = _MappedRecorder()

    with caplog.at_level(logging.WARNING, logger=base_exporter.__name__):
        assert exporter.export([span]) is SpanExportResult.SUCCESS

    assert len(exporter.exported) == 1
    assert exporter.exported[0] is span
    assert dict(span.attributes) == attributes
    assert len(_mapping_warnings(caplog)) == 1
    assert secret not in caplog.text


def test_a_second_exporter_on_the_same_provider_sees_the_span_untouched() -> None:
    # Another exporter on the provider (LiveKit Cloud's, on any LiveKit version) gets the very span
    # object ours was handed. Our mapped copies must never show up on it: LiveKit redacts by the
    # "pii" segment of a key, and input.value / user.id carry none.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    ours = _MappedRecorder()
    other = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ours))
    provider.add_span_processor(SimpleSpanProcessor(other))
    secret_attributes = {
        "lk.pii.user_input": "my card is 4111",
        "lk.pii.room_name": "room-secret",
        "lk.pii.participant_identity": "caller-secret",
    }

    provider.get_tracer("t").start_span("agent_turn", attributes=secret_attributes).end()
    provider.shutdown()

    assert SpanAttributes.INPUT_VALUE in ours.exported[0].attributes
    (seen,) = other.get_finished_spans()
    assert dict(seen.attributes) == secret_attributes
