"""Real-client privacy regressions; all requests use offline synthetic transports."""

import asyncio
import copy
import inspect
import json

import httpx
import openai
import pytest

from .test_privacy_and_raw_streams import configured_tracing
from .test_sdk_lifecycle import BASE_URL, MODEL, TOKEN

PROMPT = "SYNTHETIC-PRIVATE-PROMPT"
ANSWER = "SYNTHETIC-PRIVATE-ANSWER"
IMAGE = "data:image/png;base64,SYNTHETIC-PRIVATE-IMAGE"
STOP = "SYNTHETIC-PRIVATE-STOP"
SECRET = "SYNTHETIC-TRANSPORT-SECRET"
USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
MESSAGES = [{"role": "user", "content": [
    {"type": "text", "text": PROMPT},
    {"type": "image_url", "image_url": {"url": IMAGE}},
]}]
COMPLETION = {
    "id": "chat-privacy", "object": "chat.completion", "created": 1, "model": MODEL,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                 "finish_reason": "stop"}], "usage": USAGE,
}


def transport(requests):
    def respond(request):
        body = json.loads(request.content)
        requests.append({"body": body, "headers": request.headers, "url": request.url})
        if body.get("stream"):
            chunks = [
                {**COMPLETION, "object": "chat.completion.chunk", "usage": None,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": ANSWER},
                              "finish_reason": None}]},
                {**COMPLETION, "object": "chat.completion.chunk", "choices": []},
            ]
            data = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=(data + "data: [DONE]\n\n").encode())
        return httpx.Response(200, json=COMPLETION, headers={"x-request-id": "synthetic-request"})
    return httpx.MockTransport(respond)


def call_client(asynchronous, mode, requests, **kwargs):
    """Exercise SDK APIs, including legacy sync parse on the async raw client."""
    streaming = mode.endswith("stream")
    raw = mode.startswith("raw")
    params = {"model": MODEL, "messages": copy.deepcopy(MESSAGES), "stop": STOP,
              "stream": streaming, **kwargs}

    def check_raw(response):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream" if streaming else "application/json")
        assert response.http_response.status_code == 200

    if asynchronous:
        async def run():
            async with openai.AsyncOpenAI(
                api_key=TOKEN, base_url=BASE_URL, max_retries=0,
                http_client=httpx.AsyncClient(transport=transport(requests)),
            ) as client:
                api = client.chat.completions
                response = await (api.with_raw_response if raw else api).create(**params)
                if raw:
                    check_raw(response)
                    parsed = response.parse()
                    response_value = await parsed if inspect.isawaitable(parsed) else parsed
                    again = response.parse()
                    assert (await again if inspect.isawaitable(again) else again) is response_value
                else:
                    response_value = response
                if streaming:
                    chunks = [chunk async for chunk in response_value]
                    assert chunks[0].choices[0].delta.content == ANSWER
                else:
                    assert response_value.choices[0].message.content == ANSWER
                    assert response_value.usage.total_tokens == 5
                    if raw:
                        assert json.loads(response.http_response.content) == COMPLETION
        asyncio.run(run())
    else:
        with openai.OpenAI(
            api_key=TOKEN, base_url=BASE_URL, max_retries=0,
            http_client=httpx.Client(transport=transport(requests)),
        ) as client:
            api = client.chat.completions
            response = (api.with_raw_response if raw else api).create(**params)
            if raw:
                check_raw(response)
                response_value = response.parse()
                assert response.parse() is response_value
            else:
                response_value = response
            if streaming:
                chunks = list(response_value)
                assert chunks[0].choices[0].delta.content == ANSWER
            else:
                assert response_value.choices[0].message.content == ANSWER
                assert response_value.usage.total_tokens == 5
                if raw:
                    assert json.loads(response.http_response.content) == COMPLETION


def exported(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.is_ok
    return span.attributes, str((dict(span.attributes), span.events, span.status.description))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source", ["extra_headers", "extra_query", "extra_body"])
def test_raw_each_transport_secret(asynchronous, source):
    requests = []
    with configured_tracing() as exporter:
        call_client(asynchronous, "raw", requests, **{source: {"credential": SECRET}})
        attrs, everything = exported(exporter)
        assert SECRET not in everything
        assert TOKEN not in everything
        assert "_options" not in attrs["output.value"]
        assert json.loads(attrs["output.value"])["choices"][0]["message"]["content"] == ANSWER
        assert attrs["gen_ai.usage.total_tokens"] == 5
    request = requests[0]
    actual = (request["headers"]["credential"] if source == "extra_headers" else
              request["url"].params["credential"] if source == "extra_query" else
              request["body"]["credential"])
    assert actual == SECRET


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["normal", "stream", "raw", "raw_stream"])
def test_visible_output_and_usage(asynchronous, mode):
    with configured_tracing() as exporter:
        call_client(asynchronous, mode, [])
        attrs, everything = exported(exporter)
        assert attrs["gen_ai.usage.total_tokens"] == 5
        assert attrs["gen_ai.request.model"] == MODEL
        assert PROMPT in attrs["input.value"]
        assert ANSWER in attrs["output.value"]
        assert TOKEN not in everything


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["normal", "stream", "raw", "raw_stream"])
@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.parametrize("policy,hidden", [
    ("hide_inputs", [PROMPT, IMAGE, STOP]),
    ("hide_input_messages", [PROMPT, IMAGE]),
    ("hide_input_text", [PROMPT]),
    ("hide_input_images", [IMAGE]),
    ("hide_llm_invocation_parameters", [STOP]),
    ("hide_outputs", [ANSWER]),
    ("hide_output_messages", [ANSWER]),
    ("hide_output_text", [ANSWER]),
    ("base64_image_max_length", [IMAGE]),
])
def test_individual_privacy_flags_all_exported_copies(
    monkeypatch, asynchronous, mode, environment, policy, hidden,
):
    value = 1 if policy == "base64_image_max_length" else True
    config = {policy: value}
    if environment:
        monkeypatch.setenv("FI_" + policy.upper(), str(value).lower())
        config = {}
    requests = []
    messages = copy.deepcopy(MESSAGES)
    with configured_tracing(**config) as exporter:
        call_client(asynchronous, mode, requests, messages=messages)
        assert messages == MESSAGES
        attrs, everything = exported(exporter)
        for sentinel in hidden:
            assert sentinel not in everything
        if policy.startswith("hide_output"):
            assert PROMPT in attrs["input.value"]
            assert IMAGE in attrs["input.value"]
            assert STOP in attrs["input.value"]
        else:
            assert ANSWER in attrs["output.value"]
        if policy in ("hide_input_images", "base64_image_max_length", "hide_llm_invocation_parameters"):
            assert PROMPT in attrs["input.value"]
        assert attrs["gen_ai.request.model"] == MODEL
        assert attrs["gen_ai.usage.total_tokens"] == 5
    assert requests[0]["body"]["messages"] == MESSAGES
    assert requests[0]["body"]["stop"] == STOP


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["normal", "stream", "raw", "raw_stream"])
@pytest.mark.parametrize("kind", ["iterator", "generator"])
def test_one_shot_messages_match_native_request(asynchronous, mode, kind):
    def messages():
        copied = copy.deepcopy(MESSAGES)
        return iter(copied) if kind == "iterator" else (message for message in copied)

    native, instrumented = [], []
    native_messages, traced_messages = messages(), messages()
    try:
        call_client(asynchronous, mode, native, messages=native_messages)
    except TypeError as error:
        # SDK 1.0 cannot serialize these iterables. Preserve its error AND the
        # unconsumed caller-owned input, rather than changing either behavior.
        assert openai.__version__ == "1.0.0"
        assert "is not JSON serializable" in str(error)
        with configured_tracing(), pytest.raises(TypeError) as traced_error:
            call_client(asynchronous, mode, instrumented, messages=traced_messages)
        assert str(traced_error.value) == str(error)
        assert list(traced_messages) == list(native_messages) == MESSAGES
        assert not native and not instrumented
        return
    with configured_tracing() as exporter:
        call_client(asynchronous, mode, instrumented, messages=traced_messages)
        attrs, _ = exported(exporter)
        assert attrs["gen_ai.usage.total_tokens"] == 5
        assert "messages" not in json.loads(attrs["input.value"])
    assert native[0]["body"]["messages"] == MESSAGES
    assert instrumented[0]["body"] == native[0]["body"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["normal", "stream", "raw", "raw_stream"])
@pytest.mark.parametrize("content", [PROMPT, [{"type": "text", "text": PROMPT}]])
def test_plain_and_multipart_text_privacy_without_request_mutation(asynchronous, mode, content):
    messages = [{"role": "user", "content": copy.deepcopy(content)}]
    original = copy.deepcopy(messages)
    requests = []
    with configured_tracing(hide_input_text=True) as exporter:
        call_client(asynchronous, mode, requests, messages=messages)
        _, everything = exported(exporter)
        assert PROMPT not in everything
        assert ANSWER in everything
    assert messages == original
    assert requests[0]["body"]["messages"] == original


def test_unknown_response_internals_are_never_serialized():
    from types import SimpleNamespace
    from traceai_fireworks._wrappers import _get_output_attributes

    response = SimpleNamespace(_options={"credential": SECRET, "messages": MESSAGES})
    assert _get_output_attributes(response) == {}
