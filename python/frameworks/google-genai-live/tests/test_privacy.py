"""Privacy (J7; AC-14, AC-18, AC-19, AC-20)."""

from __future__ import annotations

import pytest
from google.genai import errors, types
from fi_instrumentation import TraceConfig

from ._voice_tab import assert_voice_tab
from .conftest import API_KEY, AUDIO_CONFIG, AUDIO_MARKER, HANDLE_1, all_text, load, run_turn, spans_by_name

MODEL = "gemini-3.8-live"
MARK = "FI-PRIVACY-MARKER-31d7"
SYSTEM = "You are a weather bot. " + MARK


def _config():
    return dict(AUDIO_CONFIG, system_instruction=SYSTEM, session_resumption={},
                tools=[{"function_declarations": [{"name": "get_weather", "description": "weather " + MARK}]}])


async def _run(client, server):
    server.scripts.append(load("j2_tool"))
    async with client.aio.live.connect(model=MODEL, config=_config()) as session:
        await session.send_client_content(turns={"role": "user", "parts": [{"text": "Weather in " + MARK}]})
        await session.send_realtime_input(text="realtime " + MARK)
        await run_turn(session, tool_result={"result": "sunny " + MARK})
        await run_turn(session)


async def test_default_captures_text_but_never_audio_key_or_handle(server, instrument, client, exporter, log_text):
    instrument()
    server.scripts.append(load("j1_realtime_audio"))
    async with client.aio.live.connect(model=MODEL, config=dict(_config())) as session:
        await session.send_realtime_input(audio=types.Blob(data=AUDIO_MARKER.encode() * 20,
                                                           mime_type="audio/pcm;rate=16000"))
        await run_turn(session)
    text = all_text(exporter)
    assert MARK in text  # the system instruction is content and is on by default
    assert AUDIO_MARKER not in text  # AC-19: inbound and outbound audio bytes
    assert API_KEY not in text and API_KEY not in log_text()  # AC-20
    assert HANDLE_1 not in text and HANDLE_1 not in log_text()  # AC-14
    assert "generativelanguage.googleapis.com" not in text  # host is not recorded, path only
    assert_voice_tab(exporter.get_finished_spans(), forbidden_substrings=(API_KEY, HANDLE_1, AUDIO_MARKER))


HIDE_BOTH = {"FI_HIDE_INPUTS": "true", "FI_HIDE_OUTPUTS": "true"}


def _hide(monkeypatch, env=HIDE_BOTH):
    for name, value in env.items():
        monkeypatch.setenv(name, value)


async def test_ac18_hide_flags_omit_all_content(server, instrument, client, exporter, monkeypatch):
    _hide(monkeypatch)
    instrument()
    await _run(client, server)
    text = all_text(exporter)
    assert MARK not in text
    by = spans_by_name(exporter)
    for span in exporter.get_finished_spans():
        for key in ("input.value", "output.value", "conversation.transcript", "fi.conversation.transcript"):
            assert key not in (span.attributes or {}), (span.name, key)
    # Metadata stays.
    (tool,) = by["gemini.live.tool"]
    assert tool.attributes["tool.name"] == "get_weather"
    root = by["gemini.live.session"][0]
    assert root.attributes["gemini.tool_names"] == ("get_weather",)
    model = by["gemini.live.model_turn"][0]
    assert model.attributes["llm.token_count.prompt"] == 616
    assert model.attributes["llm.model_name"] == MODEL
    assert root.attributes["call.total_turns"] >= 2


async def test_full_mode_captures_each_content_kind(server, instrument, client, exporter):
    instrument()
    await _run(client, server)
    by = spans_by_name(exporter)
    assert MARK in by["gemini.live.setup"][0].attributes["input.value"]
    assert MARK in by["gemini.live.tool"][0].attributes["output.value"]
    assert any(MARK in (u.attributes.get("input.value") or "") for u in by["gemini.live.user_turn"])
    assert MARK in by["gemini.live.session"][0].attributes["conversation.transcript"]


@pytest.mark.parametrize("hide", ["inputs", "outputs"])
async def test_trace_config_hides_one_direction(server, instrument, client, exporter, hide):
    cfg = TraceConfig(hide_inputs=hide == "inputs", hide_outputs=hide == "outputs")
    instrument(config=cfg)
    await _run(client, server)
    by = spans_by_name(exporter)
    tool = by["gemini.live.tool"][0]
    if hide == "inputs":
        assert "input.value" not in tool.attributes and "output.value" in tool.attributes
        assert "input.value" not in by["gemini.live.setup"][0].attributes
    else:
        assert "output.value" not in tool.attributes and "input.value" in tool.attributes
    # The transcript needs both directions.
    assert "conversation.transcript" not in by["gemini.live.session"][0].attributes


async def test_env_hide_inputs_is_honoured(server, instrument, client, exporter, monkeypatch):
    instrument()
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    # TraceConfig reads the environment when instrument() builds it, so re-instrument.
    from traceai_google_genai_live import GoogleGenAILiveInstrumentor

    GoogleGenAILiveInstrumentor().uninstrument()
    instrument()
    await _run(client, server)
    assert "input.value" not in spans_by_name(exporter)["gemini.live.setup"][0].attributes


async def test_trace_config_both_sides_equals_env(server, instrument, client, exporter):
    instrument(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    await _run(client, server)
    assert MARK not in all_text(exporter)


@pytest.mark.parametrize("env", [
    {"FI_HIDE_INPUT_MESSAGES": "true", "FI_HIDE_OUTPUT_MESSAGES": "true"},
    {"FI_HIDE_INPUT_TEXT": "true", "FI_HIDE_OUTPUT_TEXT": "true"},
])
async def test_message_and_text_switches_drop_live_content(server, instrument, client, exporter,
                                                           monkeypatch, env):
    # Live content is all message text, so the narrower standard switches drop it too.
    _hide(monkeypatch, env)
    instrument()
    await _run(client, server)
    assert MARK not in all_text(exporter)
    assert spans_by_name(exporter)["gemini.live.tool"][0].attributes["tool.name"] == "get_weather"


async def test_one_narrow_switch_drops_only_its_side(server, instrument, client, exporter, monkeypatch):
    _hide(monkeypatch, {"FI_HIDE_OUTPUT_TEXT": "true"})
    instrument()
    await _run(client, server)
    tool = spans_by_name(exporter)["gemini.live.tool"][0]
    assert "output.value" not in tool.attributes and "input.value" in tool.attributes
    assert MARK in spans_by_name(exporter)["gemini.live.setup"][0].attributes["input.value"]


def test_no_package_specific_privacy_switch():
    import inspect

    from traceai_google_genai_live import instrument as shorthand

    assert "metadata_only" not in inspect.signature(shorthand).parameters


async def test_error_text_is_content(server, instrument, client, exporter, monkeypatch):
    _hide(monkeypatch)
    instrument()
    server.scripts.append([["close", 1008, "models/" + MARK + " is not found"]])
    with pytest.raises(errors.APIError):
        async with client.aio.live.connect(model=MARK, config=AUDIO_CONFIG):
            pass
    text = all_text(exporter)
    # The model string is metadata and stays; the server's error text does not.
    assert "is not found" not in text
    setup = spans_by_name(exporter)["gemini.live.setup"][0]
    assert setup.attributes["gemini.close_code"] == 1008
    assert setup.attributes["exception.type"] == "APIError"
    assert "exception.stacktrace" not in text


async def test_error_text_kept_in_full_mode_without_stacktrace(server, instrument, client, exporter):
    instrument()
    server.scripts.append([["close", 1008, "models/x is not found"]])
    with pytest.raises(errors.APIError):
        async with client.aio.live.connect(model="x", config=AUDIO_CONFIG):
            pass
    text = all_text(exporter)
    assert "is not found" in text
    assert "Traceback" not in text and "stacktrace" not in text
