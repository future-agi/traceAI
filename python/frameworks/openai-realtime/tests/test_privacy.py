"""Privacy: the standard traceAI hide switches, audio bytes, credentials, error text."""

from __future__ import annotations

import argparse
import asyncio
import logging

import pytest
from fi_instrumentation import TraceConfig
from openai import AsyncOpenAI

import voice_agent
from conftest import API_KEY, AUDIO_PREFIX, DROP_ERRORS, MARKER, by_name, load, replay, root_of
from traceai_openai_realtime._session import RESPONSE_NAME, TOOL_NAME, TURN_NAME
from voice_tab import all_values, assert_voice_tab

# Published core 1.1 retains the legacy descriptions; current core owns error privacy.
NATIVE_ERROR_POLICY = hasattr(TraceConfig(), "hide_error_details")

CONTENT_KEYS = (
    "input.value",
    "output.value",
    "conversation.transcript",
    "fi.conversation.transcript",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "error.message",
    "exception.message",
)
LEAKS = (MARKER, "@example.com", "/Users/alice", "Paris", "weather assistant")


def _content_keys(spans):
    found = set()
    for span in spans:
        for key in span.attributes:
            if key in CONTENT_KEYS or key.startswith("conversation.transcript.") or key.endswith(".message.content") \
                    or key.endswith(".function.arguments"):
                found.add(key)
        for event in span.events:
            found.update(k for k in event.attributes if k in CONTENT_KEYS)
    return found


def _args(**kw):
    base = dict(model="gpt-realtime-2.1", marker=MARKER, user_id=None, text=False,
                error_case=True, playback_speed=6.0, mic_pace=0.002)
    base.update(kw)
    return argparse.Namespace(**base)


def test_hiding_inputs_and_outputs_drops_all_content_but_keeps_the_shape(make_recorder, exporter):
    """AC-13 / J6: no transcripts, instructions, tool arguments or results, error text."""
    recorder = make_recorder(capture_input=False, capture_output=False)
    errors = load("j5_errors")
    for row in errors:  # both fixtures restart the stub's event ids
        if row["event"].get("event_id"):
            row["event"]["event_id"] = "j5-" + row["event"]["event_id"]
    replay(recorder, load("j1_voice_call") + errors)
    recorder.finish("client_closed")
    spans = exporter.get_finished_spans()
    assert _content_keys(spans) == set()
    blob = all_values(spans)
    for needle in LEAKS:
        assert needle not in blob, needle
    root = root_of(spans).attributes
    assert root["fi.span.kind"] == "CONVERSATION"
    assert root["gen_ai.request.model"] == "gpt-realtime-2.1"
    assert root["realtime.voice"] == "marin"
    assert root["call.total_turns"] == 7
    (tool,) = by_name(spans, TOOL_NAME)
    assert tool.attributes["gen_ai.tool.name"] == "get_weather"
    assert any("gen_ai.usage.input_tokens" in s.attributes for s in by_name(spans, RESPONSE_NAME))
    assert {s.attributes.get("error.code") for s in spans if s.name == "realtime.error"} >= {"invalid_event"}


@pytest.mark.parametrize(
    "env",
    [
        {"FI_HIDE_INPUTS": "true", "FI_HIDE_OUTPUTS": "true"},
        {"FI_HIDE_INPUT_MESSAGES": "true", "FI_HIDE_OUTPUT_MESSAGES": "true"},
        {"FI_HIDE_INPUT_TEXT": "true", "FI_HIDE_OUTPUT_TEXT": "true"},
    ],
    ids=["inputs-outputs", "messages", "text"],
)
def test_hide_switches_through_the_sdk_including_errors_and_a_socket_drop(env, monkeypatch, instrument, exporter,
                                                                        stub_server):
    """The same through the real SDK, with an error event and a close reason that both carry the marker.
    The switch is the standard traceAI one (FI_HIDE_* read by TraceConfig); there is no second one."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    instrument()
    server = stub_server(drop_after_deltas=4)
    with pytest.raises(DROP_ERRORS):
        asyncio.run(voice_agent.voice_call(AsyncOpenAI(api_key=API_KEY, websocket_base_url=server.url), _args()))
    spans = exporter.get_finished_spans()
    assert _content_keys(spans) == set()
    blob = all_values(spans)
    for needle in LEAKS + (API_KEY,):
        assert needle not in blob, needle
    root = root_of(spans)
    assert root.attributes["realtime.end_reason"] == "socket_closed"
    assert root.status.description == (None if NATIVE_ERROR_POLICY else "ConnectionClosedError")
    assert root.status.status_code.name == "ERROR"
    (event,) = [event for event in root.events if event.name == "exception"]
    assert event.attributes["exception.type"].endswith("ConnectionClosedError")
    assert "exception.message" not in event.attributes
    assert "exception.stacktrace" not in event.attributes


def test_full_mode_carries_content(instrument, exporter, stub_server):
    """The default captures content, like every traceAI instrumentor (the opt-out is FI_HIDE_*)."""
    instrument()
    server = stub_server()
    asyncio.run(voice_agent.voice_call(AsyncOpenAI(api_key=API_KEY, websocket_base_url=server.url), _args()))
    spans = exporter.get_finished_spans()
    blob = all_values(spans)
    assert MARKER in blob and "@example.com" in blob and "/Users/alice-" in blob
    assert {"input.value", "output.value", "conversation.transcript", "gen_ai.tool.call.arguments",
            "gen_ai.tool.call.result", "error.message"} <= _content_keys(spans)


def test_audio_bytes_never_on_a_span(make_recorder, exporter, instrument, stub_server):
    """AC-14: no attribute value contains the fixture's base64 audio, even with full capture."""
    recorder = make_recorder()
    replay(recorder, load("j1_voice_call") + load("push_to_talk")[1:])
    recorder.finish("client_closed")
    assert AUDIO_PREFIX not in all_values(exporter.get_finished_spans())

    exporter.clear()
    instrument()
    server = stub_server()
    asyncio.run(voice_agent.voice_call(AsyncOpenAI(api_key=API_KEY, websocket_base_url=server.url), _args(error_case=False)))
    tone = voice_agent.base64.b64encode(voice_agent.pcm(100, True)).decode()[:48]
    silence = voice_agent.base64.b64encode(b"\x00" * 600).decode()
    blob = all_values(exporter.get_finished_spans())
    assert tone not in blob and silence not in blob


def test_credentials_never_on_a_span_or_in_a_log(make_recorder, exporter, caplog):
    """AC-15: the client's key, the subprotocol form and bearer headers are scrubbed from
    error text; nothing the package logs carries them."""
    caplog.set_level(logging.DEBUG)
    recorder = make_recorder()
    replay(recorder, load("j5_errors"))
    recorder.on_server_frame(b"{not json " + API_KEY.encode())
    recorder.finish("client_closed")
    spans = exporter.get_finished_spans()
    assert_voice_tab(spans, forbidden_substrings=(API_KEY, "ek_fixture1234567"))
    (keyed,) = [s for s in spans if s.attributes.get("error.code") == "invalid_api_key"]
    assert "[REDACTED]" in keyed.attributes["error.message"]
    assert API_KEY not in caplog.text and "ek_fixture" not in caplog.text


def test_key_in_url_or_safety_identifier_never_recorded(instrument, exporter, stub_server):
    """A key in the websocket URL query and the OpenAI-Safety-Identifier header never reach a span."""
    instrument()
    server = stub_server()
    client = AsyncOpenAI(
        api_key=API_KEY,
        websocket_base_url=f"{server.url}?api_key={API_KEY}",
        default_headers={"OpenAI-Safety-Identifier": "hashed-user-77aa"},
    )

    async def session():
        async with client.realtime.connect(model="gpt-realtime-2.1") as conn:
            async for event in conn:
                if event.type == "session.created":
                    break

    asyncio.run(session())
    blob = all_values(exporter.get_finished_spans())
    assert API_KEY not in blob and "hashed-user-77aa" not in blob and "127.0.0.1" not in blob
    assert "user.id" not in root_of(exporter.get_finished_spans()).attributes


@pytest.mark.parametrize(
    "env, kept, dropped",
    [
        ({"FI_HIDE_INPUTS": "true"}, "assistant", "user"),
        ({"FI_HIDE_OUTPUTS": "true"}, "user", "assistant"),
        ({"FI_HIDE_INPUT_MESSAGES": "true"}, "assistant", "user"),
        ({"FI_HIDE_OUTPUT_MESSAGES": "true"}, "user", "assistant"),
    ],
)
def test_trace_config_env_flags(env, kept, dropped, monkeypatch, instrument, exporter, stub_server):
    """FI_HIDE_INPUTS drops what the user said, instructions and tool arguments;
    FI_HIDE_OUTPUTS drops what the model said, tool results and error text.
    Either one drops the combined conversation transcript."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    instrument()
    server = stub_server()
    asyncio.run(voice_agent.voice_call(AsyncOpenAI(api_key=API_KEY, websocket_base_url=server.url), _args()))
    spans = exporter.get_finished_spans()
    root = root_of(spans).attributes
    assert "conversation.transcript" not in root
    turns = by_name(spans, TURN_NAME)
    responses = [s for s in by_name(spans, RESPONSE_NAME) if s.attributes.get("realtime.audio_delta_count")]
    (tool,) = by_name(spans, TOOL_NAME)
    if dropped == "user":
        assert all("output.value" not in t.attributes for t in turns)
        assert all("input.value" not in r.attributes for r in responses)
        assert "input.value" not in tool.attributes and "input.value" not in root
        assert all("output.value" in r.attributes for r in responses)
        assert "gen_ai.tool.call.result" in tool.attributes
    else:
        assert all("output.value" in t.attributes for t in turns)
        assert all("output.value" not in r.attributes for r in responses)
        assert "gen_ai.tool.call.result" not in tool.attributes
        assert not [s for s in spans if "error.message" in s.attributes]
        assert "input.value" in tool.attributes


def test_trace_config_object_is_the_same_switch(instrument, exporter, stub_server):
    """TraceConfig(hide_inputs=True, hide_outputs=True) passed in code equals the env vars."""
    from fi_instrumentation import TraceConfig

    instrument(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    server = stub_server()
    asyncio.run(voice_agent.voice_call(AsyncOpenAI(api_key=API_KEY, websocket_base_url=server.url), _args()))
    spans = exporter.get_finished_spans()
    assert _content_keys(spans) == set()
    blob = all_values(spans)
    for needle in LEAKS + (API_KEY,):
        assert needle not in blob, needle
    assert root_of(spans).attributes["fi.span.kind"] == "CONVERSATION"
