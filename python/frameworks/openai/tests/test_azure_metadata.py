"""Azure metadata through real SDK clients and mock HTTP transports only."""

import asyncio
import json

import openai
import pytest
from fi_instrumentation.fi_types import SpanAttributes
from traceai_openai import OpenAIInstrumentor, set_client_provider

from .test_azure_provider import ANSWER, DEPLOYMENT as RESPONSE_MODEL, azure_response
from .test_openai_compatible_spans import make_tracer, only_span  # noqa: F401
from .test_vercel_gateway import sdk_httpx

API_VERSION = "2024-10-21"
REQUEST_MODEL = "request-deployment"
CLIENT_DEPLOYMENT = "client-deployment"
MESSAGES = [{"role": "user", "content": "Say hello."}]


def call_chat(
    exporter, handler, client_kwargs, *, use_async=False, stream=False,
    configure=None, client_types=(openai.AzureOpenAI, openai.AsyncAzureOpenAI),
    model=REQUEST_MODEL,
):
    """Keep SDK transport types compatible with both supported SDK generations."""
    kwargs = {"api_key": "synthetic-key", "max_retries": 0, **client_kwargs}
    request = {"model": model, "messages": MESSAGES, "stream": stream}

    def run_sync():
        with client_types[0](**kwargs, http_client=sdk_httpx.Client(
            transport=sdk_httpx.MockTransport(handler), trust_env=False,
        )) as client:
            if configure:
                configure(client)
            response = client.chat.completions.create(**request)
            if stream:
                assert not exporter.get_finished_spans(), "Do not finish before consumption"
                answer = "".join(c.choices[0].delta.content or "" for c in response if c.choices)
            else:
                answer = response.choices[0].message.content
            assert answer == ANSWER

    async def run_async():
        async with client_types[1](**kwargs, http_client=sdk_httpx.AsyncClient(
            transport=sdk_httpx.MockTransport(handler), trust_env=False,
        )) as client:
            if configure:
                configure(client)
            response = await client.chat.completions.create(**request)
            if stream:
                assert not exporter.get_finished_spans(), "Do not finish before consumption"
                answer = "".join([
                    c.choices[0].delta.content or "" async for c in response if c.choices
                ])
            else:
                answer = response.choices[0].message.content
            assert answer == ANSWER

    asyncio.run(run_async()) if use_async else run_sync()


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("stream", [False, True], ids=["chat", "stream"])
@pytest.mark.parametrize("deployment", [None, CLIENT_DEPLOYMENT], ids=["request", "client"])
@pytest.mark.parametrize("host,port,provider", [
    ("RESOURCE.OPENAI.AZURE.COM", None, "azure"),
    ("resource.openai.azure.us", 8443, "azure"),
    ("GATEWAY.INVALID", 9443, "openai"),
])
def test_azure_request_metadata(make_tracer, use_async, stream, deployment, host, port, provider):
    exporter = make_tracer()
    seen = []
    endpoint = f"https://{host}" + (f":{port}" if port else "")
    routed_deployment = deployment or REQUEST_MODEL

    def handler(request):
        seen.append(request)
        assert request.url.host == host.lower()
        assert request.url.port == port
        assert request.url.path == f"/openai/deployments/{routed_deployment}/chat/completions"
        assert request.url.params["api-version"] == API_VERSION
        assert request.url.params["opaque"] == "synthetic-query-value"
        assert request.headers["api-key"] == "synthetic-key"
        assert json.loads(request.content)["model"] == REQUEST_MODEL
        return azure_response("stream" if stream else "success")

    call_chat(exporter, handler, {
        "azure_endpoint": endpoint, "azure_deployment": deployment,
        "api_version": API_VERSION, "default_query": {"opaque": "synthetic-query-value"},
    }, use_async=use_async, stream=stream)

    _, attrs = only_span(exporter)
    assert len(seen) == 1
    assert attrs[SpanAttributes.SERVER_ADDRESS] == host.lower()
    if port is None:
        assert "server.port" not in attrs
    else:
        assert type(attrs["server.port"]) is int
        assert attrs["server.port"] == port
    assert attrs["azure.openai.api_version"] == API_VERSION
    assert attrs["azure.openai.deployment"] == routed_deployment
    assert attrs["gen_ai.provider.name"] == provider
    # Preserve the instrumentor's existing model contract independently of routing.
    assert attrs["gen_ai.request.model"] == (REQUEST_MODEL if stream else RESPONSE_MODEL)
    serialized = json.dumps(attrs)
    for forbidden in ("synthetic-key", "synthetic-query-value", "opaque", "api-key", "https://", "api-version="):
        assert forbidden not in serialized


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_azure_v1_base_url_without_api_version(make_tracer, use_async):
    exporter = make_tracer()
    seen = []

    def without_version(client):
        # These SDK versions require a constructor api_version even for base_url.
        # Exercise a versionless default query without falling back to _api_version.
        client._custom_query.pop("api-version")

    def handler(request):
        seen.append(request)
        assert "api-version" not in request.url.params
        return azure_response("success")

    call_chat(exporter, handler, {
        "base_url": "https://resource.openai.azure.com/openai/v1/",
        "api_version": API_VERSION,
    }, use_async=use_async, configure=without_version)
    _, attrs = only_span(exporter)
    assert len(seen) == 1
    assert attrs[SpanAttributes.SERVER_ADDRESS] == "resource.openai.azure.com"
    assert attrs["azure.openai.deployment"] == REQUEST_MODEL
    assert "azure.openai.api_version" not in attrs
    assert "server.port" not in attrs


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_azure_api_version_uses_current_default_query(make_tracer, use_async):
    exporter = make_tracer()
    sent_version = "2025-01-01-preview"

    def configure(client):
        client._custom_query["api-version"] = sent_version

    def handler(request):
        assert request.url.params["api-version"] == sent_version
        return azure_response("success")

    call_chat(exporter, handler, {
        "azure_endpoint": "https://resource.openai.azure.us", "api_version": API_VERSION,
    }, use_async=use_async, configure=configure)
    _, attrs = only_span(exporter)
    assert attrs["azure.openai.api_version"] == sent_version


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("suffix", ["openai.azure.com", "openai.azure.us"])
def test_plain_openai_clients_do_not_get_azure_metadata(make_tracer, use_async, suffix):
    exporter = make_tracer()
    call_chat(exporter, lambda _: azure_response("success"), {
        "base_url": f"https://resource.{suffix}/openai/v1/",
        "default_query": {"api-version": API_VERSION},
    }, use_async=use_async, client_types=(openai.OpenAI, openai.AsyncOpenAI))
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.provider.name"] == "azure"
    assert not any(key.startswith("azure.openai.") for key in attrs)
    assert SpanAttributes.SERVER_ADDRESS not in attrs
    assert "server.port" not in attrs


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_azure_metadata_preserves_explicit_provider_override(make_tracer, use_async):
    exporter = make_tracer()
    call_chat(exporter, lambda _: azure_response("success"), {
        "azure_endpoint": "https://resource.openai.azure.com", "api_version": API_VERSION,
    }, use_async=use_async, configure=lambda client: set_client_provider(client, provider="custom"))
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.provider.name"] == "custom"
    assert attrs["azure.openai.deployment"] == REQUEST_MODEL
    assert attrs["azure.openai.api_version"] == API_VERSION
    assert attrs[SpanAttributes.SERVER_ADDRESS] == "resource.openai.azure.com"


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_azure_endpoint_metadata_excludes_url_credentials_and_query(make_tracer, use_async):
    exporter = make_tracer()
    call_chat(exporter, lambda _: azure_response("success"), {
        "base_url": (
            "https://synthetic-user:synthetic-password@GATEWAY.INVALID:8443/"
            "openai/v1/?opaque=synthetic-query-value"
        ),
        "api_version": API_VERSION,
        "default_headers": {"Authorization": "Bearer synthetic-entra-token"},
    }, use_async=use_async)
    _, attrs = only_span(exporter)
    assert attrs[SpanAttributes.SERVER_ADDRESS] == "gateway.invalid"
    assert attrs["server.port"] == 8443
    serialized = json.dumps(attrs)
    for forbidden in (
        "synthetic-key", "synthetic-user", "synthetic-password", "synthetic-query-value",
        "synthetic-entra-token", "opaque", "https://", "/openai/", "Authorization", "api-key",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("model", [openai.NOT_GIVEN, None, ""], ids=["absent", "none", "empty"])
def test_azure_missing_deployment_is_omitted(make_tracer, use_async, model):
    exporter = make_tracer()
    call_chat(exporter, lambda _: azure_response("success"), {
        "azure_endpoint": "https://resource.openai.azure.com", "api_version": API_VERSION,
    }, use_async=use_async, model=model)
    _, attrs = only_span(exporter)
    assert attrs[SpanAttributes.SERVER_ADDRESS] == "resource.openai.azure.com"
    assert attrs["azure.openai.api_version"] == API_VERSION
    assert "azure.openai.deployment" not in attrs


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_azure_metadata_failure_does_not_break_request_or_span(make_tracer, use_async):
    class UnreadableQuery(dict):
        def get(self, key, default=None):
            if key == "api-version":
                raise RuntimeError("synthetic-metadata-failure")
            return super().get(key, default)

    class QueryMixin:
        @property
        def default_query(self):
            # SDK mapping merges still work; optional metadata lookup fails.
            return UnreadableQuery(super().default_query)

    class SyncClient(QueryMixin, openai.AzureOpenAI):
        pass

    class AsyncClient(QueryMixin, openai.AsyncAzureOpenAI):
        pass

    exporter = make_tracer()
    call_chat(exporter, lambda _: azure_response("success"), {
        "azure_endpoint": "https://resource.openai.azure.com", "api_version": API_VERSION,
    }, use_async=use_async, client_types=(SyncClient, AsyncClient))
    _, attrs = only_span(exporter)
    assert attrs["gen_ai.provider.name"] == "azure"
    assert attrs["gen_ai.request.model"] == RESPONSE_MODEL
    assert not any(key.startswith("azure.openai.") for key in attrs)
    assert "synthetic-metadata-failure" not in json.dumps(attrs)


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_uninstrument_restores_azure_requests(make_tracer, use_async):
    originals = (openai.AzureOpenAI.request, openai.AsyncAzureOpenAI.request)
    exporter = make_tracer()
    seen = []

    def handler(request):
        seen.append(request)
        return azure_response("success")

    kwargs = {"azure_endpoint": "https://resource.openai.azure.us", "api_version": API_VERSION}
    call_chat(exporter, handler, kwargs, use_async=use_async)
    _, attrs = only_span(exporter)
    assert attrs["azure.openai.deployment"] == REQUEST_MODEL
    OpenAIInstrumentor().uninstrument()
    assert (openai.AzureOpenAI.request, openai.AsyncAzureOpenAI.request) == originals
    exporter.clear()
    call_chat(exporter, handler, kwargs, use_async=use_async)
    assert len(seen) == 2
    assert not exporter.get_finished_spans()
