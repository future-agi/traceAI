import json

import google.generativeai as genai
import pytest
from fi_instrumentation import TraceConfig
from google.generativeai import protos

from conftest import MODEL, call_response, one, text_response

PROMPT, RESPONSE, ARGS, SYSTEM, TOOLDEF, RESULT = (
    "PROMPT-MARK-1f", "RESPONSE-MARK-2e", "ARGS-MARK-3d", "SYSTEM-MARK-4c", "TOOLDEF-MARK-5b", "RESULT-MARK-6a")


def _run(client, tool_call=False):
    def lookup(city: str):
        """TOOLDEF-MARK-5b lookup"""

    m = genai.GenerativeModel(MODEL, system_instruction=SYSTEM, tools=[lookup])
    m._client = client
    client.responses.append(call_response("lookup", {"city": ARGS}) if tool_call else text_response(RESPONSE))
    m.generate_content([
        {"role": "user", "parts": [PROMPT]},
        {"role": "user", "parts": [protos.Part(function_response=protos.FunctionResponse(name="lookup", response={"r": RESULT}))]},
    ])


def _dump(exporter):
    return json.dumps(dict(one(exporter)[1]), default=str)


def test_content_is_present_without_a_hide_flag(spans, client):
    _run(client)
    dumped = _dump(spans)
    for marker in (PROMPT, RESPONSE, SYSTEM, TOOLDEF, RESULT):
        assert marker in dumped, marker


def test_tool_arguments_are_present_without_a_hide_flag(spans, client):
    _run(client, tool_call=True)
    assert ARGS in _dump(spans)


def test_hide_inputs_removes_prompt_system_tools_and_results(make_spans, client):
    exporter = make_spans(config=TraceConfig(hide_inputs=True))
    _run(client)
    dumped = _dump(exporter)
    for marker in (PROMPT, SYSTEM, TOOLDEF, RESULT):
        assert marker not in dumped, marker
    assert RESPONSE in dumped
    assert json.loads(dumped)["gen_ai.request.model"] == MODEL


def test_hide_outputs_removes_response_and_tool_arguments(make_spans, client):
    exporter = make_spans(config=TraceConfig(hide_outputs=True))
    _run(client, tool_call=True)
    dumped = _dump(exporter)
    assert ARGS not in dumped
    assert PROMPT in dumped


def test_hide_outputs_removes_streamed_text(make_spans, client):
    exporter = make_spans(config=TraceConfig(hide_outputs=True))
    m = genai.GenerativeModel(MODEL)
    m._client = client
    client.chunks = [text_response(RESPONSE[:5], usage=None), text_response(RESPONSE[5:])]
    for _ in m.generate_content(PROMPT, stream=True):
        pass
    dumped = _dump(exporter)
    assert RESPONSE not in dumped and RESPONSE[:5] not in dumped
    assert PROMPT in dumped


@pytest.mark.parametrize("env", ["FI_HIDE_INPUTS", "FI_HIDE_INPUT_MESSAGES"])
def test_hide_inputs_from_environment(make_spans, client, monkeypatch, env):
    monkeypatch.setenv(env, "true")
    exporter = make_spans()
    _run(client)
    dumped = _dump(exporter)
    assert PROMPT not in dumped and SYSTEM not in dumped


def test_hide_inputs_removes_embedding_text(make_spans, client):
    exporter = make_spans(config=TraceConfig(hide_inputs=True))
    genai.embed_content(model="models/text-embedding-004", content=PROMPT, client=client)
    assert PROMPT not in _dump(exporter)


TITLE = "TITLE-MARK-7f"


@pytest.mark.parametrize("flags", [{}, {"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}])
def test_embedding_title_follows_the_embedding_text(make_spans, client, flags):
    exporter = make_spans(config=TraceConfig(**flags))
    genai.embed_content(model="models/text-embedding-004", content=PROMPT, title=TITLE, task_type="retrieval_document", client=client)
    dumped = _dump(exporter)
    if flags:
        assert TITLE not in dumped and PROMPT not in dumped
    else:
        assert TITLE in dumped and PROMPT in dumped
