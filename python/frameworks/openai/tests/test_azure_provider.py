"""Azure cloud attribution through the real SDK and a loopback OTLP receiver.

Government endpoint: https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure
Only HTTP transports are faked; no vendor service is called. JSON uses the
shared test exporter, not a new production transport.
"""

import asyncio
import json

import openai
import pytest
from traceai_openai._providers import provider_from_host

from .test_openai_compatible_spans import responses_body
from .test_parallel_provider import wire_tracer  # noqa: F401
from .test_vercel_gateway import sdk_httpx

DEPLOYMENT = "synthetic-azure-deployment"
API_KEY = "synthetic-azure-key"
PROMPT = "Say hello."
ANSWER = "Hello from Azure."


@pytest.mark.parametrize(
    "host,expected",
    [
        ("resource.openai.azure.us", "azure"),
        ("RESOURCE.OPENAI.AZURE.US.", "azure"),
        ("resource.openai.azure.com", "azure"),
        ("resource.services.ai.azure.com", "azure"),
        ("resource.cognitiveservices.azure.com", "azure"),
        ("resource.openai.azure.us.customer.example", "openai"),
        ("resource.notopenai.azure.us", "openai"),
        ("resource-openai.azure.us", "openai"),
        ("openai.azure.us", "openai"),
        ("customer.azure.us", "openai"),
        ("localhost", "openai"),
        (None, "openai"),
    ],
)
def test_azure_documented_hosts_only(host, expected):
    assert provider_from_host(host) == expected


def azure_response(outcome):
    if outcome == "error":
        return sdk_httpx.Response(
            401, json={"error": {"message": "Synthetic authentication failure"}}
        )
    if outcome == "responses":
        return sdk_httpx.Response(200, json=responses_body(text=ANSWER, model=DEPLOYMENT))
    if outcome == "embeddings":
        return sdk_httpx.Response(200, json={
            "object": "list", "model": DEPLOYMENT,
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        })
    common = {"id": "synthetic-chat", "created": 1, "model": DEPLOYMENT}
    usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    if outcome == "stream":
        chunks = [
            {**common, "object": "chat.completion.chunk", "choices": [{
                "index": 0, "delta": {"role": "assistant", "content": ANSWER},
                "finish_reason": "stop",
            }]},
            {**common, "object": "chat.completion.chunk", "choices": [], "usage": usage},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return sdk_httpx.Response(
            200, headers={"Content-Type": "text/event-stream"},
            content=(body + "data: [DONE]\n\n").encode(),
        )
    return sdk_httpx.Response(200, json={
        **common, "object": "chat.completion", "usage": usage,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": ANSWER}}],
    })


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compress", [False, True], ids=["plain", "gzip"])
@pytest.mark.parametrize("suffix", ["openai.azure.com", "openai.azure.us"])
@pytest.mark.parametrize("legacy", [False, True], ids=["standard-client", "azure-client"])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["success", "stream", "error", "embeddings", "responses"])
def test_azure_provider_on_otlp_wire(
    wire_tracer, encoding, compress, suffix, legacy, use_async, outcome
):
    payloads = wire_tracer(encoding, compress, False)
    seen = []
    host = f"resource.{suffix}"

    def handler(request):
        seen.append(request)
        assert request.url.host == host
        assert request.headers["api-key" if legacy else "authorization"] == (
            API_KEY if legacy else f"Bearer {API_KEY}"
        )
        body = json.loads(request.content)
        assert body["model"] == DEPLOYMENT
        if legacy:
            assert request.url.params["api-version"] == "2024-10-21"
        assert "x-api-key" not in request.headers
        assert "x-secret-key" not in request.headers
        return azure_response(outcome)

    kwargs = {"api_key": API_KEY, "max_retries": 0}
    if legacy:
        kwargs.update(azure_endpoint=f"https://{host}", api_version="2024-10-21")
    else:
        kwargs["base_url"] = f"https://{host}/openai/v1/"

    def request(sdk):
        if outcome == "embeddings":
            return sdk.embeddings.create(model=DEPLOYMENT, input=PROMPT, encoding_format="float")
        if outcome == "responses":
            return sdk.responses.create(model=DEPLOYMENT, input=PROMPT)
        args = {"model": DEPLOYMENT, "messages": [{"role": "user", "content": PROMPT}]}
        if outcome == "stream":
            args.update(stream=True, stream_options={"include_usage": True})
        return sdk.chat.completions.create(**args)

    def assert_response(response):
        if outcome == "embeddings":
            assert response.data[0].embedding == [0.1, 0.2]
        elif outcome == "responses":
            assert response.output_text == ANSWER
        else:
            assert response.choices[0].message.content == ANSWER

    def run_sync():
        client_class = openai.AzureOpenAI if legacy else openai.OpenAI
        with client_class(**kwargs, http_client=sdk_httpx.Client(
            transport=sdk_httpx.MockTransport(handler), trust_env=False
        )) as sdk:
            response = request(sdk)
            if outcome == "stream":
                assert not payloads, "Do not export before the stream is consumed"
                assert "".join(c.choices[0].delta.content or "" for c in response if c.choices) == ANSWER
            else:
                assert_response(response)

    async def run_async():
        client_class = openai.AsyncAzureOpenAI if legacy else openai.AsyncOpenAI
        async with client_class(**kwargs, http_client=sdk_httpx.AsyncClient(
            transport=sdk_httpx.MockTransport(handler), trust_env=False
        )) as sdk:
            response = await request(sdk)
            if outcome == "stream":
                assert not payloads, "Do not export before the stream is consumed"
                assert "".join([c.choices[0].delta.content or "" async for c in response if c.choices]) == ANSWER
            else:
                assert_response(response)

    def run():
        asyncio.run(run_async()) if use_async else run_sync()

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            run()
    else:
        run()
    assert len(seen) == len(payloads) == 1
    path, headers, payload = payloads[0]
    assert path == "/v1/traces"
    assert headers.get("Content-Encoding") == ("gzip" if compress else None)
    assert headers["Content-Type"] == (
        "application/json" if encoding == "json" else "application/x-protobuf"
    )
    spans = [s for resource in payload["resourceSpans"]
             for scope in resource["scopeSpans"] for s in scope["spans"]]
    assert len(spans) == 1, "The shared OpenAI instrumentor must not duplicate Azure spans"
    span = spans[0]
    attrs = {a["key"]: a["value"] for a in span["attributes"]}
    assert attrs["gen_ai.provider.name"] == {"stringValue": "azure"}
    assert attrs["gen_ai.request.model"] == {"stringValue": DEPLOYMENT}
    assert span["status"]["code"] == (2 if outcome == "error" else 1)
    if outcome != "error":
        assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "5"}
    assert API_KEY not in json.dumps(payload)
