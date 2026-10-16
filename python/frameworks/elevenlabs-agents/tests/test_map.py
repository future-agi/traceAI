import json

import pytest

from conftest import MARKER, load_fixture
from traceai_elevenlabs_agents.map import MappingInputError, attributes_to_dict, iter_spans, map_otlp_traces
from voice_tab_check import assert_voice_tab


def mapped(name="post_call_transcription_otel.json", capture_content=False, **kwargs):
    body = load_fixture(name)
    data = body["data"] if "data" in body else body
    return map_otlp_traces(
        data["otlp_traces"],
        conversation_id=data.get("conversation_id"),
        agent_id=data.get("agent_id"),
        project_name=kwargs.pop("project_name", "th8231-unit"),
        capture_content=capture_content,
        **kwargs,
    )


def by_name(export_request):
    out = {}
    for resource, span in iter_spans(export_request):
        out.setdefault(span["name"], []).append((resource, span, attributes_to_dict(span["attributes"])))
    return out


def test_ids_names_times_and_hierarchy_are_preserved():
    source = load_fixture("post_call_transcription_otel.json")["data"]["otlp_traces"]
    source_spans = source["resourceSpans"][0]["scopeSpans"][0]["spans"]
    request, summary = mapped()
    out_spans = [s for _, s in iter_spans(request)]
    assert len(out_spans) == len(source_spans) == summary["span_count"] == 8
    for src, out in zip(source_spans, out_spans, strict=True):
        assert (out["traceId"], out["spanId"], out.get("parentSpanId"), out["name"]) == (
            src["traceId"],
            src["spanId"],
            src.get("parentSpanId"),
            src["name"],
        )
        assert out["startTimeUnixNano"] == src["startTimeUnixNano"]
        assert out["endTimeUnixNano"] == src["endTimeUnixNano"]
    assert summary["trace_ids"] == ["4bf92f3577b34da6a3ce929d0e0e4736"]
    # AC-04: the tool span's parent is the agent-response span ElevenLabs set.
    tool = by_name(request)["elevenlabs.tool.get_exchange_rate"][0][1]
    assert tool["parentSpanId"] == "a000000000000004"


def test_root_is_parentless_conversation_with_voice_tab_attributes():
    request, _ = mapped()
    _, root, attrs = by_name(request)["elevenlabs.conversation"][0]
    assert "parentSpanId" not in root
    assert attrs["fi.span.kind"] == "CONVERSATION"
    assert attrs["gen_ai.system"] == "elevenlabs"
    assert attrs["session.id"] == "conv_9001k1zph3fkeh5s8xg9z90swaqa"
    assert attrs["call.duration"] == 42.0
    assert attrs["call.total_turns"] == 5
    assert attrs["elevenlabs.agent_id"] == "agent_7101k5zvyjhmfg983brhmhkd98n6"
    assert attrs["elevenlabs.source"] == "post_call_webhook"
    assert attrs["elevenlabs.cost_fiat"] == 0.0421
    assert "call.talk_ratio" not in attrs  # AC-07: no per-side audio durations in the payload
    # Only the root is a conversation.
    kinds = [a.get("fi.span.kind") for _, _, a in sum(by_name(request).values(), [])]
    assert kinds.count("CONVERSATION") == 1
    assert_voice_tab(
        [s for _, s in iter_spans(request)],
        omissions=("call.talk_ratio", "user.id", "conversation.transcript", "fi.conversation.transcript"),
        forbidden_substrings=(MARKER,),
    )


def test_content_off_exports_no_content_anywhere():
    request, summary = mapped(capture_content=False)
    blob = json.dumps(request)
    assert MARKER not in blob
    for needle in ("alice", "/Users/", "/home/", "example.com", "X-Goog-Signature", "someone-elses-project"):
        assert needle not in blob
    assert summary["transcript_rows"] == 0
    for _, span in iter_spans(request):
        attrs = attributes_to_dict(span["attributes"])
        for key in ("input.value", "output.value", "conversation.transcript", "fi.conversation.transcript", "user.id"):
            assert key not in attrs
        assert not any(k.startswith("conversation.transcript.") for k in attrs)


def test_content_off_keeps_structure_model_and_tokens():
    request, _ = mapped(capture_content=False)
    names = by_name(request)
    llm_turns = [a for _, _, a in names["elevenlabs.recv.agent_response"] if a["fi.span.kind"] == "LLM"]
    assert len(llm_turns) == 2
    first = llm_turns[0]
    assert first["gen_ai.request.model"] == "gpt-4o-mini"
    assert first["gen_ai.usage.input_tokens"] == 412
    assert first["gen_ai.usage.output_tokens"] == 23
    assert first["elevenlabs.rag.embedding_model"] == "multilingual_e5_large_instruct"
    assert first["elevenlabs.rag.latency_ms"] == 41.5
    assert "elevenlabs.reasoning_content" not in first
    assert "elevenlabs.rag.retrieval_query" not in first
    greeting = names["elevenlabs.recv.agent_response"][0][2]
    assert greeting["fi.span.kind"] == "CHAIN"
    assert all(a["fi.span.kind"] == "CHAIN" for _, _, a in names["elevenlabs.recv.user_transcript"])
    tool = names["elevenlabs.tool.get_exchange_rate"][0][2]
    assert tool["fi.span.kind"] == "TOOL"
    assert tool["tool.name"] == "get_exchange_rate"
    assert all(a["session.id"] == "conv_9001k1zph3fkeh5s8xg9z90swaqa" for _, _, a in sum(names.values(), []))


def test_content_on_writes_transcript_and_io():
    request, summary = mapped(capture_content=True, user_id="user-42")
    names = by_name(request)
    attrs = names["elevenlabs.conversation"][0][2]
    transcript = json.loads(attrs["conversation.transcript"])
    assert attrs["fi.conversation.transcript"] == attrs["conversation.transcript"]
    assert [r["role"] for r in transcript] == ["assistant", "user", "assistant", "user", "assistant"]
    assert transcript[1]["content"] == "What is the US dollar to euro rate today? " + MARKER
    assert transcript[1]["time"] == "4"
    assert summary["transcript_rows"] == 5
    for i, row in enumerate(transcript):
        assert attrs["conversation.transcript.%d.message.role" % i] == row["role"]
        assert attrs["conversation.transcript.%d.message.content" % i] == row["content"]
    assert attrs["user.id"] == "user-42"
    assert attrs["input.value"].startswith("What is the US dollar")
    assert attrs["output.value"] == "Goodbye! " + MARKER
    tool = names["elevenlabs.tool.get_exchange_rate"][0][2]
    assert json.loads(tool["input.value"])["base"] == "USD"
    assert json.loads(tool["output.value"])["rates"]["EUR"] == 0.92
    llm = [a for _, _, a in names["elevenlabs.recv.agent_response"] if a["fi.span.kind"] == "LLM"][0]
    assert llm["input.value"] == "What is the US dollar to euro rate today? " + MARKER
    assert llm["output.value"] == "One US dollar is 0.92 euros right now. " + MARKER
    assert llm["elevenlabs.reasoning_content"].endswith(MARKER)
    # Signed media URLs are dropped in both modes (D-V03).
    assert "X-Goog-Signature" not in json.dumps(request)
    assert "elevenlabs.recording_url" not in attrs
    assert_voice_tab([s for _, s in iter_spans(request)], omissions=("call.talk_ratio",))


def test_turn_order_uses_turn_index_when_present():
    body = load_fixture("post_call_transcription_otel.json")
    spans = body["data"]["otlp_traces"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    for span in spans:  # whole-second timestamps: make every turn start in the same second
        if span["name"].startswith("elevenlabs.recv."):
            span["startTimeUnixNano"] = spans[0]["startTimeUnixNano"]
    spans[1], spans[2] = spans[2], spans[1]
    request, _ = map_otlp_traces(
        body["data"]["otlp_traces"], conversation_id=None, project_name="p", capture_content=True
    )
    attrs = by_name(request)["elevenlabs.conversation"][0][2]
    assert [r["role"] for r in json.loads(attrs["conversation.transcript"])][:2] == ["assistant", "user"]


def test_no_turn_text_omits_transcript_and_uses_time_in_call_fallback():
    request, _ = mapped("post_call_transcription_otel_no_text.json", capture_content=True)
    attrs = by_name(request)["elevenlabs.conversation"][0][2]
    for key in ("conversation.transcript", "fi.conversation.transcript", "input.value", "output.value"):
        assert key not in attrs
    assert not any(k.startswith("conversation.transcript.") for k in attrs)
    assert attrs["call.duration"] == 7.0  # zero-length root, numeric time_in_call_secs on the root
    assert attrs["call.total_turns"] == 2


def test_root_without_turns_omits_turn_count_and_duration():
    otlp = {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "1" * 32,
                                "spanId": "2" * 16,
                                "name": "elevenlabs.conversation",
                                "startTimeUnixNano": "1760000000000000000",
                                "endTimeUnixNano": "1760000000000000000",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    request, _ = map_otlp_traces(otlp, conversation_id="conv_x", project_name="p")
    attrs = attributes_to_dict(next(iter_spans(request))[1]["attributes"])
    assert "call.total_turns" not in attrs and "call.duration" not in attrs
    assert attrs["session.id"] == "conv_x"


def test_error_status_preserved_and_message_dropped_when_content_off():
    request, _ = mapped("post_call_transcription_otel_error.json", capture_content=False)
    names = by_name(request)
    root = names["elevenlabs.conversation"][0][1]
    tool = names["elevenlabs.tool.book_table"][0][1]
    assert root["status"] == {"code": 2}
    assert tool["status"] == {"code": 2}
    assert tool["events"] == [{"name": "exception", "timeUnixNano": "1760000116000000000"}]
    assert MARKER not in json.dumps(request)


def test_error_status_message_and_event_kept_when_content_on():
    request, _ = mapped("post_call_transcription_otel_error.json", capture_content=True)
    names = by_name(request)
    assert names["elevenlabs.conversation"][0][1]["status"]["message"].startswith("call ended")
    event = names["elevenlabs.tool.book_table"][0][1]["events"][0]
    assert attributes_to_dict(event["attributes"])["exception.message"].startswith("ReadTimeout")
    transcript = json.loads(names["elevenlabs.conversation"][0][2]["conversation.transcript"])
    assert transcript[-1]["content"] == "Sure, let me check..." + MARKER  # interruption stays as sent


def test_unknown_span_name_passes_through_without_kind():
    request, _ = mapped(capture_content=False)
    unknown = by_name(request)["elevenlabs.event.something_new"][0][2]
    assert "fi.span.kind" not in unknown
    assert unknown["elevenlabs.event.count"] == 3  # numeric metadata survives content-off
    assert "elevenlabs.event.data" not in unknown


def test_four_kb_tool_argument_is_stored_as_sent():
    body = load_fixture("post_call_transcription_otel.json")
    big = '{"q": "' + ("x" * 4087) + '"}'
    assert len(big) == 4096
    for span in body["data"]["otlp_traces"]["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        if span["name"] == "elevenlabs.tool.get_exchange_rate":
            span["attributes"][0]["value"]["stringValue"] = big
    request, _ = map_otlp_traces(body["data"]["otlp_traces"], conversation_id=None, project_name="p", capture_content=True)
    tool = by_name(request)["elevenlabs.tool.get_exchange_rate"][0][2]
    assert tool["elevenlabs.tool.params"] == big
    assert tool["input.value"] == big
    assert "elevenlabs.truncated" not in tool


def test_payload_cannot_choose_the_project():
    request, _ = mapped(project_name="bound-project")
    for resource, _ in iter_spans(request):
        assert resource["project_name"] == "bound-project"
        assert resource["project_type"] == "observe"
        assert not any(k.startswith("fi.") for k in resource)
    other, _ = mapped(project_name="second-project")
    assert {r["project_name"] for r, _ in iter_spans(other)} == {"second-project"}


@pytest.mark.parametrize(
    "otlp, code",
    [
        (None, "missing_resource_spans"),
        ({}, "missing_resource_spans"),
        ({"resourceSpans": []}, "missing_resource_spans"),
        ({"resourceSpans": [{"scopeSpans": [{"spans": []}]}]}, "missing_resource_spans"),
        ({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "zz", "spanId": "1" * 16}]}]}]}, "invalid_span"),
        ({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "1" * 32, "spanId": "1"}]}]}]}, "invalid_span"),
        ({"resourceSpans": ["x"]}, "invalid_resource_spans"),
    ],
)
def test_malformed_payloads_raise_stable_codes(otlp, code):
    with pytest.raises(MappingInputError) as info:
        map_otlp_traces(otlp, conversation_id="conv_x", project_name="p")
    assert info.value.code == code


def test_missing_conversation_id_everywhere_is_an_input_error():
    otlp = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "1" * 32, "spanId": "2" * 16, "name": "x"}]}]}]}
    with pytest.raises(MappingInputError) as info:
        map_otlp_traces(otlp, conversation_id=None, project_name="p")
    assert info.value.code == "missing_conversation_id"
