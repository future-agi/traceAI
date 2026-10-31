"""Perplexity's documented Responses extensions, without vendor/model calls.

The real OpenAI SDK uses MockTransport; a real OTLP exporter serializes each
span into protobuf before its HTTP transport is intercepted and decoded.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
import requests
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_openai import OpenAIInstrumentor
from traceai_openai._request import _parse_request_args


@pytest.fixture
def wire(monkeypatch):
    payloads = []
    providers = []

    def send(_session, request, **kwargs):
        assert request.url == "http://127.0.0.1:4318/v1/traces"
        message = ExportTraceServiceRequest()
        message.ParseFromString(request.body)
        payloads.append(message)
        response = requests.Response()
        response.status_code = 200
        response._content = b""
        return response

    monkeypatch.setattr(requests.Session, "send", send)

    def start(config=None):
        provider = TracerProvider()
        provider.add_span_processor(
            SimpleSpanProcessor(
                OTLPSpanExporter(
                    endpoint="http://127.0.0.1:4318/v1/traces",
                    session=requests.Session(),
                )
            )
        )
        providers.append(provider)
        OpenAIInstrumentor().instrument(
            tracer_provider=provider, config=config or TraceConfig()
        )
        return payloads

    yield start
    OpenAIInstrumentor().uninstrument()
    for provider in providers:
        provider.shutdown()


def attributes(payloads):
    spans = [
        span
        for payload in payloads
        for resource in payload.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    assert len(spans) == 1
    assert spans[0].name == "Response"
    return {
        item.key: getattr(item.value, item.value.WhichOneof("value"))
        for item in spans[0].attributes
    }


def response_body():
    return {
        "id": "resp_synthetic",
        "object": "response",
        "created_at": 1,
        "model": "served-model",
        "status": "completed",
        "tools": [],
        "output": [
            {
                "type": "search_results",
                "queries": ["private-query"],
                "results": [
                    {
                        "id": 1,
                        "title": "private-title",
                        "url": "https://example.invalid/private-url",
                        "snippet": "private-snippet",
                        "date": "2026-01-01",
                        "source": "web",
                    },
                ],
            },
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "private-answer", "annotations": []}
                ],
            },
        ],
        # Perplexity extensions must work even without OpenAI's token-detail objects.
        "usage": {
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
            "cost": {
                "currency": "USD",
                "input_cost": 0.01,
                "output_cost": 0.02,
                "total_cost": 0.04,
                "cache_creation_cost": 0.0,
                "cache_read_cost": 0.005,
                "tool_calls_cost": 0.005,
            },
        },
    }


def run_call(body, *, async_mode=False, streaming=False, extra=None, status=200):
    sent = []

    def handle(request):
        assert request.url.host == "api.perplexity.ai"
        assert request.url.path == "/v1/responses"
        assert request.url.params["synthetic"] == "private-query-option"
        sent.append(json.loads(request.content))
        if streaming and status == 200:
            events = [
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 0,
                    "item_id": "msg_1",
                    "output_index": 1,
                    "content_index": 0,
                    "delta": "private-answer",
                    "logprobs": [],
                },
                {"type": "response.completed", "sequence_number": 1, "response": body},
            ]
            data = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=data
            )
        return httpx.Response(status, json=body)

    params = {
        "model": "requested-model",
        "input": "private-input",
        "stream": streaming,
        "extra_body": extra if extra is not None else {"effort": "low"},
        "extra_headers": {"X-Test-Secret": "private-header"},
        "extra_query": {"synthetic": "private-query-option"},
    }
    options = {
        "base_url": "https://api.perplexity.ai/v1",
        "api_key": "synthetic-key",
        "max_retries": 0,
    }

    async def call_async():
        async with openai.AsyncOpenAI(
            **options,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handle),
                trust_env=False,
            ),
        ) as client:
            result = await client.responses.create(**params)
            if streaming:
                return [event async for event in result]
            return result

    if async_mode:
        result = asyncio.run(call_async())
    else:
        with openai.OpenAI(
            **options,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handle),
                trust_env=False,
            ),
        ) as client:
            result = client.responses.create(**params)
            if streaming:
                result = list(result)
    return sent, result


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_extensions_reach_otlp(wire, async_mode, streaming):
    payloads = wire()
    sent, result = run_call(response_body(), async_mode=async_mode, streaming=streaming)
    attrs = attributes(payloads)
    assert sent[0]["effort"] == "low"
    assert json.loads(attrs["gen_ai.request.parameters"])["effort"] == "low"
    assert attrs["gen_ai.provider.name"] == "perplexity"
    assert attrs["gen_ai.request.model"] == "served-model"
    assert attrs["gen_ai.usage.total_tokens"] == 5
    assert attrs["gen_ai.cost.total"] == 0.04
    assert attrs["gen_ai.cost.input"] == 0.01
    assert attrs["gen_ai.cost.output"] == 0.02
    assert attrs["gen_ai.cost.cache_write"] == 0.0
    assert attrs["gen_ai.cost.cache_read"] == 0.005
    assert attrs["gen_ai.cost.tool_calls"] == 0.005
    assert attrs["gen_ai.cost.currency"] == "USD"
    assert attrs["retrieval.documents.0.document.id"] == "1"
    assert attrs["retrieval.documents.0.document.content"] == "private-snippet"
    metadata = json.loads(attrs["retrieval.documents.0.document.metadata"])
    assert metadata == {
        "title": "private-title",
        "url": "https://example.invalid/private-url",
        "date": "2026-01-01",
        "source": "web",
    }
    assert "private-header" not in str(attrs)
    assert "private-query-option" not in str(attrs)
    # No wrapping/replacement of SDK results and no duplicate retrieval spans.
    response = result[-1].response if streaming else result
    assert response.output_text == "private-answer"
    assert response.usage.cost["total_cost"] == 0.04


@pytest.mark.parametrize("streaming", [False, True])
def test_output_privacy_covers_search_results_on_wire(wire, streaming):
    payloads = wire(TraceConfig(hide_outputs=True))
    run_call(response_body(), streaming=streaming)
    attrs = attributes(payloads)
    for marker in (
        "private-query",
        "private-title",
        "private-url",
        "private-snippet",
        "private-answer",
    ):
        assert marker not in str(payloads)
    assert not any(key.startswith("retrieval.documents.") for key in attrs)
    assert attrs["gen_ai.cost.total"] == 0.04
    assert attrs["gen_ai.usage.total_tokens"] == 5


@pytest.mark.parametrize(
    "config",
    [TraceConfig(hide_inputs=True), TraceConfig(hide_llm_invocation_parameters=True)],
)
def test_invocation_privacy_covers_extra_body(wire, config):
    payloads = wire(config)
    run_call(
        response_body(),
        extra={"effort": "low", "metadata": {"private-extra": "secret"}},
    )
    assert "private-extra" not in str(payloads)
    if config.hide_inputs:
        assert "private-input" not in str(payloads)


def test_extra_body_overrides_without_mutating_options():
    from openai._types import Omit

    base = {
        "model": "old",
        "input": "old-input",
        "metadata": {"old": "value"},
        "temperature": 1,
    }
    extra = {
        "model": "new",
        "input": "new-input",
        "metadata": {"new": "value"},
        "temperature": Omit(),
        "effort": "low",
    }
    options = SimpleNamespace(
        json_data=base, extra_json=extra, headers={"authorization": "secret"}
    )
    cast_to, params = _parse_request_args((openai.types.responses.Response, options))
    assert cast_to is openai.types.responses.Response
    assert params == {
        "model": "new",
        "input": "new-input",
        "metadata": {"new": "value"},
        "effort": "low",
    }
    assert base["model"] == "old" and base["temperature"] == 1
    assert isinstance(extra["temperature"], Omit)


@pytest.mark.parametrize(
    "cost",
    [
        None,
        {},
        "bad",
        {"total_cost": "1"},
        {"total_cost": True},
        {"total_cost": -1},
        {"total_cost": float("nan")},
        {"total_cost": float("inf")},
        {"total_cost": 10**1000},
    ],
)
def test_invalid_or_missing_cost_is_not_fabricated(cost):
    from traceai_openai._response_attributes_extractor import (
        _ResponseAttributesExtractor,
    )

    body = response_body()
    body["usage"]["cost"] = cost
    response = openai.types.responses.Response.construct(**body)
    attrs = dict(
        _ResponseAttributesExtractor(openai).get_attributes_from_response(response, {})
    )
    assert "gen_ai.cost.total" not in attrs
    assert attrs["gen_ai.usage.total_tokens"] == 5


@pytest.mark.parametrize(
    "output",
    [
        None,
        [],
        [None, "bad", {}],
        [{"type": "search_results", "results": None}],
        [{"type": "search_results", "results": [None, "bad", {}]}],
    ],
)
def test_missing_or_malformed_search_does_not_invent_documents(output):
    from traceai_openai._attributes._response_extensions import (
        response_extension_attributes,
    )

    attrs = dict(response_extension_attributes({"output": output}, TraceConfig()))
    assert attrs == {}


def test_multiple_search_items_keep_distinct_documents():
    from traceai_openai._attributes._response_extensions import (
        response_extension_attributes,
    )

    response = {
        "output": [
            {
                "type": "search_results",
                "results": [None, {"id": 1, "snippet": "first"}],
            },
            {"type": "message"},
            {"type": "search_results", "results": [{"id": 2, "snippet": "second"}]},
        ]
    }
    attrs = dict(response_extension_attributes(response, TraceConfig()))
    assert attrs == {
        "retrieval.documents.0.document.id": "1",
        "retrieval.documents.0.document.content": "first",
        "retrieval.documents.1.document.id": "2",
        "retrieval.documents.1.document.content": "second",
    }


def test_extra_body_override_matches_the_sent_request(wire):
    payloads = wire()
    sent, _ = run_call(
        response_body(),
        extra={
            "model": "overridden-model",
            "input": "overridden-input",
            "effort": "high",
        },
    )
    attrs = attributes(payloads)
    params = json.loads(attrs["gen_ai.request.parameters"])
    assert params["model"] == sent[0]["model"] == "overridden-model"
    assert params["effort"] == sent[0]["effort"] == "high"
    assert attrs["input.value"] == sent[0]["input"] == "overridden-input"
    assert "private-input" not in str(attrs)
    assert "input" not in params


def test_failed_requests_keep_sent_effort(wire):
    payloads = wire()
    with pytest.raises(openai.BadRequestError):
        run_call({"error": {"message": "synthetic rejection"}}, status=400)
    attrs = attributes(payloads)
    assert json.loads(attrs["gen_ai.request.parameters"])["effort"] == "low"
    assert attrs["gen_ai.request.model"] == "requested-model"
    assert not any(key.startswith("gen_ai.cost.") for key in attrs)
