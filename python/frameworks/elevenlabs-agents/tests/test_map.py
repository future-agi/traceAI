import json
from dataclasses import fields

import pytest
from fi_instrumentation import TraceConfig

from conftest import MARKER, NEVER_NEEDLES, hide_all, load_fixture
from traceai_elevenlabs_agents import privacy
from traceai_elevenlabs_agents.map import MappingInputError, attributes_to_dict, iter_spans, map_otlp_traces
from voice_tab_check import assert_voice_tab

CONTENT_KEYS = ("input.value", "output.value", "conversation.transcript", "fi.conversation.transcript", "user.id")
RAW_INPUT_KEYS = ("elevenlabs.user.text", "elevenlabs.tool.params", "elevenlabs.rag.retrieval_query")
RAW_OUTPUT_KEYS = ("elevenlabs.agent.text", "elevenlabs.tool.result", "elevenlabs.reasoning_content")


def mapped(name="post_call_transcription_otel.json", trace_config=None, **kwargs):
    body = load_fixture(name)
    data = body["data"] if "data" in body else body
    return map_otlp_traces(
        data["otlp_traces"],
        conversation_id=data.get("conversation_id"),
        agent_id=data.get("agent_id"),
        project_name=kwargs.pop("project_name", "th8231-unit"),
        trace_config=trace_config if trace_config is not None else TraceConfig(),
        **kwargs,
    )


def by_name(export_request):
    out = {}
    for resource, span in iter_spans(export_request):
        out.setdefault(span["name"], []).append((resource, span, attributes_to_dict(span["attributes"])))
    return out


def all_attrs(export_request):
    return [attributes_to_dict(span["attributes"]) for _, span in iter_spans(export_request)]


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
    kinds = [a.get("fi.span.kind") for a in all_attrs(request)]
    assert kinds.count("CONVERSATION") == 1
    # Defaults: the transcript is there, and nothing from the never-exported list is.
    assert_voice_tab(
        [s for _, s in iter_spans(request)],
        omissions=("call.talk_ratio", "user.id"),  # the webhook payload carries no user id
        forbidden_substrings=NEVER_NEEDLES,
    )


def test_defaults_capture_the_conversation_like_every_traceai_instrumentor():
    request, summary = mapped(user_id="user-42")
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
    assert attrs["elevenlabs.analysis.transcript_summary"].endswith(MARKER)
    # Dynamic variables are kept, but an email address in one never leaves.
    assert attrs["elevenlabs.dynamic_variable.account_number"] == 4400123456
    assert attrs["elevenlabs.dynamic_variable.user_email"] == "<EMAIL_ADDRESS>"
    tool = names["elevenlabs.tool.get_exchange_rate"][0][2]
    assert json.loads(tool["input.value"])["base"] == "USD"
    assert json.loads(tool["output.value"])["rates"]["EUR"] == 0.92
    llm = [a for _, _, a in names["elevenlabs.recv.agent_response"] if a["fi.span.kind"] == "LLM"][0]
    assert llm["input.value"] == "What is the US dollar to euro rate today? " + MARKER
    assert llm["output.value"] == "One US dollar is 0.92 euros right now. " + MARKER
    assert llm["elevenlabs.reasoning_content"].endswith(MARKER)
    assert llm["elevenlabs.rag.retrieval_query"].endswith(MARKER)
    # Signed media URLs are dropped in every mode (D-V03).
    assert "X-Goog-Signature" not in json.dumps(request)
    assert "elevenlabs.recording_url" not in attrs
    assert_voice_tab([s for _, s in iter_spans(request)], omissions=("call.talk_ratio",))


def test_all_hide_flags_on_export_no_content_anywhere():
    request, summary = mapped(trace_config=hide_all(), user_id="user-" + MARKER)
    blob = json.dumps(request)
    assert MARKER not in blob
    for needle in NEVER_NEEDLES:
        assert needle not in blob
    assert summary["transcript_rows"] == 0
    for attrs in all_attrs(request):
        for key in CONTENT_KEYS + RAW_INPUT_KEYS + RAW_OUTPUT_KEYS:
            assert key not in attrs
        prefixes = ("conversation.transcript.", "elevenlabs.dynamic_variable", "elevenlabs.analysis")
        assert not any(k.startswith(prefixes) for k in attrs)


def test_all_hide_flags_on_keep_structure_model_and_tokens():
    request, _ = mapped(trace_config=hide_all())
    names = by_name(request)
    llm_turns = [a for _, _, a in names["elevenlabs.recv.agent_response"] if a["fi.span.kind"] == "LLM"]
    assert len(llm_turns) == 2
    first = llm_turns[0]
    assert first["gen_ai.request.model"] == "gpt-4o-mini"
    assert first["gen_ai.usage.input_tokens"] == 412
    assert first["gen_ai.usage.output_tokens"] == 23
    assert first["elevenlabs.rag.embedding_model"] == "multilingual_e5_large_instruct"
    assert first["elevenlabs.rag.latency_ms"] == 41.5
    greeting = names["elevenlabs.recv.agent_response"][0][2]
    assert greeting["fi.span.kind"] == "CHAIN"
    assert all(a["fi.span.kind"] == "CHAIN" for _, _, a in names["elevenlabs.recv.user_transcript"])
    tool = names["elevenlabs.tool.get_exchange_rate"][0][2]
    assert tool["fi.span.kind"] == "TOOL"
    assert tool["tool.name"] == "get_exchange_rate"
    root = names["elevenlabs.conversation"][0][2]
    assert (root["call.duration"], root["call.total_turns"]) == (42.0, 5)
    assert all(a["session.id"] == "conv_9001k1zph3fkeh5s8xg9z90swaqa" for a in all_attrs(request))
    # Still listed in the Voice tab, without a transcript.
    assert_voice_tab(
        [s for _, s in iter_spans(request)],
        omissions=("call.talk_ratio", "user.id", "conversation.transcript", "fi.conversation.transcript"),
        forbidden_substrings=(MARKER,),
    )


@pytest.mark.parametrize("flag", privacy.INPUT_FLAGS)
def test_any_input_flag_hides_the_user_side_only(flag):
    request, _ = mapped(trace_config=TraceConfig(**{flag: True}), user_id="user-42")
    names = by_name(request)
    for attrs in all_attrs(request):
        assert "input.value" not in attrs
        assert not any(k.startswith(RAW_INPUT_KEYS + ("elevenlabs.dynamic_variable",)) for k in attrs)
    root = names["elevenlabs.conversation"][0][2]
    assert "user.id" not in root
    assert "conversation.transcript" not in root and "fi.conversation.transcript" not in root
    # The agent side stays.
    assert root["output.value"] == "Goodbye! " + MARKER
    assert names["elevenlabs.tool.get_exchange_rate"][0][2]["output.value"].startswith('{"rates"')
    llm = [a for _, _, a in names["elevenlabs.recv.agent_response"] if a["fi.span.kind"] == "LLM"][0]
    assert llm["output.value"].endswith(MARKER) and llm["elevenlabs.reasoning_content"].endswith(MARKER)
    # Nothing the user said is left anywhere.
    assert "What is the US dollar" not in json.dumps(request) and "Great, thank you" not in json.dumps(request)


@pytest.mark.parametrize("flag", privacy.OUTPUT_FLAGS)
def test_any_output_flag_hides_the_agent_side_and_error_text(flag):
    request, _ = mapped(trace_config=TraceConfig(**{flag: True}))
    for attrs in all_attrs(request):
        assert "output.value" not in attrs
        assert not any(k.startswith(RAW_OUTPUT_KEYS + ("elevenlabs.analysis",)) for k in attrs)
    names = by_name(request)
    root = names["elevenlabs.conversation"][0][2]
    assert "conversation.transcript" not in root
    assert root["input.value"].startswith("What is the US dollar")
    assert names["elevenlabs.tool.get_exchange_rate"][0][2]["input.value"].startswith('{"base"')
    blob = json.dumps(request)
    for agent_said in ("One US dollar is", "Hi! I can help", '"rates"', "User wants an FX rate", "Caller asked for"):
        assert agent_said not in blob

    errored, _ = mapped("post_call_transcription_otel_error.json", trace_config=TraceConfig(**{flag: True}))
    error_names = by_name(errored)
    assert error_names["elevenlabs.conversation"][0][1]["status"] == {"code": 2}
    tool = error_names["elevenlabs.tool.book_table"][0][1]
    assert tool["status"] == {"code": 2}
    assert tool["events"] == [{"name": "exception", "timeUnixNano": "1760000116000000000"}]


def test_standard_env_vars_drive_the_default_trace_config(monkeypatch):
    for name in ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES"):
        monkeypatch.setenv(name, "true")
    body = load_fixture("post_call_transcription_otel.json")["data"]
    request, _ = map_otlp_traces(body["otlp_traces"], conversation_id=None, project_name="p")  # trace_config=None
    assert MARKER not in json.dumps(request)
    monkeypatch.setenv("FI_HIDE_INPUTS", "false")
    monkeypatch.setenv("FI_HIDE_INPUT_MESSAGES", "false")
    request, _ = map_otlp_traces(body["otlp_traces"], conversation_id=None, project_name="p")
    root = by_name(request)["elevenlabs.conversation"][0][2]
    assert root["input.value"].endswith(MARKER) and "output.value" not in root


@pytest.mark.parametrize("trace_config", [None, "hide_all"])
def test_never_exported_in_any_mode(trace_config):
    config = hide_all() if trace_config == "hide_all" else TraceConfig()
    for fixture in ("post_call_transcription_otel.json", "post_call_transcription_otel_error.json"):
        request, _ = mapped(fixture, trace_config=config, user_id="alice@example.com")
        blob = json.dumps(request)
        for needle in NEVER_NEEDLES:
            assert needle not in blob, (fixture, needle)
        for resource, _ in iter_spans(request):
            assert set(resource) <= privacy.RESOURCE_ALLOWLIST | {"project_name", "project_type"}
            assert resource["service.name"] == "elevenlabs-convai"
        assert all("user.id" not in a for a in all_attrs(request))  # an email-shaped user id is never written


def test_resource_allowlist_drops_host_and_process_attributes():
    request, _ = mapped()
    resource = next(iter_spans(request))[0]
    assert resource == {
        "service.name": "elevenlabs-convai",
        "elevenlabs.conversation_id": "conv_9001k1zph3fkeh5s8xg9z90swaqa",
        "project_name": "th8231-unit",
        "project_type": "observe",
    }


def test_identity_keys_on_spans_are_dropped_but_token_counts_are_not():
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
                                "attributes": [
                                    {"key": k, "value": {"stringValue": v}}
                                    for k, v in (
                                        ("host.name", "box"),
                                        ("process.command_line", "python agent.py"),
                                        ("user.email", "a@b.io"),
                                        ("enduser.id", "u"),
                                        ("code.filepath", "agent.py"),
                                        ("db.password", "pw"),
                                        ("x.api_key", "k"),
                                        ("http.request.header.authorization", "Bearer x"),
                                        ("custom.note", "kept"),
                                    )
                                ]
                                + [{"key": "gen_ai.usage.input_tokens", "value": {"intValue": "7"}}],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    request, _ = map_otlp_traces(otlp, conversation_id="conv_x", project_name="p", trace_config=TraceConfig())
    attrs = attributes_to_dict(next(iter_spans(request))[1]["attributes"])
    assert attrs["custom.note"] == "kept"
    assert attrs["gen_ai.usage.input_tokens"] == 7
    for key in ("host.name", "process.command_line", "user.email", "enduser.id", "code.filepath", "db.password",
                "x.api_key", "http.request.header.authorization"):
        assert key not in attrs


def test_error_text_is_one_scrubbed_line_by_default():
    request, _ = mapped("post_call_transcription_otel_error.json")
    names = by_name(request)
    root_status = names["elevenlabs.conversation"][0][1]["status"]
    assert root_status == {
        "code": 2,
        "message": "call ended: websocket closed abnormally " + MARKER + " <FILE_PATH> on <HOSTNAME>",
    }
    tool = names["elevenlabs.tool.book_table"][0][1]
    assert tool["status"] == {"code": 2, "message": "upstream timeout " + MARKER}
    event = attributes_to_dict(tool["events"][0]["attributes"])
    assert event == {"exception.message": "ReadTimeout " + MARKER}  # the stack trace is never forwarded
    transcript = json.loads(names["elevenlabs.conversation"][0][2]["conversation.transcript"])
    assert transcript[-1]["content"] == "Sure, let me check..." + MARKER  # interruption stays as sent


def test_stack_trace_inside_a_tool_result_is_cut_to_its_error_line():
    body = load_fixture("post_call_transcription_otel.json")
    trace = 'Traceback (most recent call last):\n  File "/Users/bob/tools/fx.py", line 9, in rate\nKeyError: \'EUR\''
    for span in body["data"]["otlp_traces"]["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        if span["name"] == "elevenlabs.tool.get_exchange_rate":
            span["attributes"][1]["value"]["stringValue"] = trace
    request, _ = map_otlp_traces(body["data"]["otlp_traces"], conversation_id=None, project_name="p")
    tool = by_name(request)["elevenlabs.tool.get_exchange_rate"][0][2]
    assert tool["output.value"] == "KeyError: 'EUR'"
    assert tool["elevenlabs.tool.result"] == "KeyError: 'EUR'"
    assert "/Users/bob" not in json.dumps(request)


def test_fi_pii_redaction_applies_to_the_content():
    if "pii_redaction" not in {f.name for f in fields(TraceConfig)}:
        pytest.skip("this fi-instrumentation-otel has no FI_PII_REDACTION")
    body = load_fixture("post_call_transcription_otel.json")
    for span in body["data"]["otlp_traces"]["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        for attribute in span.get("attributes", []):
            if attribute["key"] == "elevenlabs.user.text":
                attribute["value"]["stringValue"] = "Mail me at carol.jones@acme.io please"
                break
    plain, _ = map_otlp_traces(body["data"]["otlp_traces"], conversation_id=None, project_name="p")
    assert "carol.jones@acme.io" in json.dumps(plain)  # spoken content is content
    redacted, _ = map_otlp_traces(
        body["data"]["otlp_traces"],
        conversation_id=None,
        project_name="p",
        trace_config=TraceConfig(pii_redaction=True),
    )
    blob = json.dumps(redacted)
    assert "carol.jones@acme.io" not in blob and "<EMAIL_ADDRESS>" in blob
    root = by_name(redacted)["elevenlabs.conversation"][0][2]
    assert root["session.id"] == "conv_9001k1zph3fkeh5s8xg9z90swaqa"


def test_turn_order_uses_turn_index_when_present():
    body = load_fixture("post_call_transcription_otel.json")
    spans = body["data"]["otlp_traces"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    for span in spans:  # whole-second timestamps: make every turn start in the same second
        if span["name"].startswith("elevenlabs.recv."):
            span["startTimeUnixNano"] = spans[0]["startTimeUnixNano"]
    spans[1], spans[2] = spans[2], spans[1]
    request, _ = map_otlp_traces(body["data"]["otlp_traces"], conversation_id=None, project_name="p")
    attrs = by_name(request)["elevenlabs.conversation"][0][2]
    assert [r["role"] for r in json.loads(attrs["conversation.transcript"])][:2] == ["assistant", "user"]


def test_no_turn_text_omits_transcript_and_uses_time_in_call_fallback():
    request, _ = mapped("post_call_transcription_otel_no_text.json")
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


def test_unknown_span_name_passes_through_without_kind():
    request, _ = mapped()
    unknown = by_name(request)["elevenlabs.event.something_new"][0][2]
    assert "fi.span.kind" not in unknown
    assert unknown["elevenlabs.event.count"] == 3
    assert json.loads(unknown["elevenlabs.event.data"])["detail"] == MARKER
    hidden, _ = mapped(trace_config=hide_all())
    unknown = by_name(hidden)["elevenlabs.event.something_new"][0][2]
    assert unknown["elevenlabs.event.count"] == 3  # numeric metadata survives every mode
    assert "elevenlabs.event.data" not in unknown


def test_four_kb_tool_argument_is_stored_as_sent():
    body = load_fixture("post_call_transcription_otel.json")
    big = '{"q": "' + ("x" * 4087) + '"}'
    assert len(big) == 4096
    for span in body["data"]["otlp_traces"]["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        if span["name"] == "elevenlabs.tool.get_exchange_rate":
            span["attributes"][0]["value"]["stringValue"] = big
    request, _ = map_otlp_traces(body["data"]["otlp_traces"], conversation_id=None, project_name="p")
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


def test_trace_config_must_be_a_trace_config():
    with pytest.raises(TypeError):
        mapped(trace_config={"hide_inputs": True})


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
