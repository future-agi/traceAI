"""Stream request shapes through real SDK clients with HTTP-only stubs."""

import json

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import MessageAttributes, SpanAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from opentelemetry import trace

from .test_stream_and_embeddings_sdk import EVENTS, KEY, PROMPT, REPLY, capture, encode


HISTORY = "synthetic private history"
PREAMBLE = "synthetic private instructions"
REGRESSION_SHAPES = [
    "sdk_history", "none_history", "none_tools", "none_documents",
    "all_none", "mixed_history",
]
CONTROL_SHAPES = ["simple", "dict_history"]


def request_arguments(shape):
    kwargs = {"model": "command-r", "message": PROMPT}
    history = {"role": "USER", "message": HISTORY}
    if shape in ("sdk_history", "mixed_history"):
        # Older SDKs expose one ChatMessage model; newer ones split it by role.
        factory = getattr(cohere, "UserMessage", None) or cohere.ChatMessage
        kwargs["chat_history"] = [factory(**history)]
        if shape == "mixed_history":
            kwargs["preamble"] = PREAMBLE
            kwargs["chat_history"].append(dict(history))
    elif shape == "dict_history":
        kwargs["chat_history"] = [history]
    elif shape == "none_history":
        kwargs["chat_history"] = None
    elif shape == "none_tools":
        kwargs["tools"] = None
    elif shape == "none_documents":
        kwargs["documents"] = None
    elif shape == "all_none":
        kwargs.update(chat_history=None, tools=None, documents=None, preamble=None)
    else:
        assert shape == "simple"
    return kwargs


async def stream_call(asynchronous, kwargs, enabled=True, config=None):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "application/x-ndjson"},
            content=b"".join(encode(event) for event in EVENTS),
        )

    with capture(config, enabled) as (exporter, provider):
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                client = cohere.AsyncClient(api_key=KEY, httpx_client=http)
                events = [event async for event in client.chat_stream(**kwargs)]
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                client = cohere.Client(api_key=KEY, httpx_client=http)
                events = list(client.chat_stream(**kwargs))
        provider.force_flush()
        assert [event.event_type for event in events] == ["text-generation", "stream-end"]
        assert events[0].text == REPLY
        assert events[1].response.text == REPLY
        assert len(requests) == 1
        assert requests[0]["message"] == PROMPT
        return exporter.get_finished_spans(), requests[0]


def assert_stream(spans):
    assert len(spans) == 1, "chat_stream must export exactly one span"
    span = spans[0]
    assert span.name == "cohere.chat_stream"
    assert span.status.status_code == trace.StatusCode.OK
    assert span.attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "command-r"
    assert span.attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 2
    assert span.attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 3
    assert KEY not in json.dumps(dict(span.attributes))
    return span.attributes


def assert_messages(attributes, shape):
    expected = []
    if shape == "mixed_history":
        expected.append(("system", PREAMBLE))
    if shape in ("sdk_history", "mixed_history", "dict_history"):
        expected.append(("USER", HISTORY))
    if shape == "mixed_history":
        expected.append(("USER", HISTORY))
    expected.append(("user", PROMPT))
    prefix = SpanAttributes.GEN_AI_INPUT_MESSAGES
    for index, (role, message) in enumerate(expected):
        assert attributes[f"{prefix}.{index}.{MessageAttributes.MESSAGE_ROLE}"] == role
        assert attributes[f"{prefix}.{index}.{MessageAttributes.MESSAGE_CONTENT}"] == message
    assert sum(key.startswith(prefix + ".") for key in attributes) == 2 * len(expected)


def assert_no_extraction_errors(caplog):
    assert not [
        record for record in caplog.records
        if record.name.startswith("traceai_cohere") and record.exc_info
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shape", REGRESSION_SHAPES)
async def test_stream_request_shape_regression(asynchronous, shape, caplog):
    kwargs = request_arguments(shape)
    original_history = kwargs.get("chat_history")
    spans, body = await stream_call(asynchronous, kwargs)
    # The telemetry layer must not alter the arguments or their wire encoding.
    baseline, expected_body = await stream_call(asynchronous, kwargs, enabled=False)
    assert not baseline
    assert body == expected_body
    assert kwargs.get("chat_history") is original_history
    if shape in ("sdk_history", "mixed_history"):
        assert not isinstance(original_history[0], dict)
    attributes = assert_stream(spans)
    assert_messages(attributes, shape)
    for key in ("documents", "tools"):
        if key in kwargs:
            assert attributes[f"cohere.{key}_count"] == 0
    assert_no_extraction_errors(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shape", CONTROL_SHAPES)
async def test_stream_request_shape_control(asynchronous, shape, caplog):
    spans, _ = await stream_call(asynchronous, request_arguments(shape))
    assert_messages(assert_stream(spans), shape)
    assert_no_extraction_errors(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shape", REGRESSION_SHAPES + CONTROL_SHAPES)
async def test_uninstrumented_request_shape_control(asynchronous, shape):
    spans, _ = await stream_call(asynchronous, request_arguments(shape), enabled=False)
    assert not spans


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shape", ["sdk_history", "mixed_history"])
async def test_stream_request_shape_privacy_regression(asynchronous, shape, caplog):
    spans, _ = await stream_call(
        asynchronous, request_arguments(shape), config=TraceConfig(hide_inputs=True)
    )
    attributes = assert_stream(spans)
    assert attributes[SpanAttributes.INPUT_VALUE] == REDACTED_VALUE
    assert not any(key.startswith(SpanAttributes.GEN_AI_INPUT_MESSAGES) for key in attributes)
    serialized = json.dumps(dict(attributes))
    for value in (HISTORY, PREAMBLE, PROMPT, KEY):
        assert value not in serialized
    assert_no_extraction_errors(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("source", ["primary", "extra", "context"])
async def test_stream_extraction_failure_regression(asynchronous, source, monkeypatch):
    from traceai_cohere import _wrappers
    from traceai_cohere._request_attributes_extractor import _ChatRequestAttributesExtractor

    def broken(*args, **kwargs):
        yield SpanAttributes.GEN_AI_REQUEST_MODEL, "command-r"
        raise RuntimeError("synthetic telemetry extraction failure")

    if source == "context":
        monkeypatch.setattr(_wrappers, "get_attributes_from_context", broken)
    else:
        method = ("get_attributes_from_request" if source == "primary"
                  else "get_extra_attributes_from_request")
        monkeypatch.setattr(_ChatRequestAttributesExtractor, method, broken)
    spans, _ = await stream_call(asynchronous, request_arguments("simple"))
    assert_stream(spans)
