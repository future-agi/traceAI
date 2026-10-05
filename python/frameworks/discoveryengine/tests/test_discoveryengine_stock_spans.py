"""AC-01 (D-8332): measure the stock spans before any wrapper exists.

The architecture asks for this test first: construct ``SearchServiceClient``
with a ``unittest.mock`` transport, call ``search`` and count the spans the
process emits, with ``InMemorySpanExporter`` on the global tracer provider.
A ``control`` span around the call proves the export path delivers, so a
zero count for the library is a measurement, not a broken exporter.

The second test repeats the measurement with the real gRPC transports against
the loopback fake for every method the package may wrap (``search``,
``search_lite``, ``answer_query`` and the async twins).

google-api-core 2.40 carries an experimental, opt-in tracing path
(``GOOGLE_SDK_EXPERIMENTAL_PYTHON_TRACING_ENABLED`` plus
``opentelemetry-instrumentation-grpc``) that only traces methods whose GAPIC
transport passes ``method_name`` to ``wrap_method``. google-cloud-discoveryengine
0.20.5 does not pass it, so the measurement is repeated with the variable set.

Expected and measured result at google-cloud-discoveryengine 0.20.5: the
client emits no span, so it has no attribute keys to report.
"""

from __future__ import annotations

import asyncio
import importlib.util
from unittest import mock

import grpc
import pytest

from _discoveryengine_support import (
    SEARCH_RESULTS,
    FakeDiscoveryEngine,
    Traced,
    answer_client,
    answer_request,
    async_answer_client,
    async_search_client,
    new_provider,
    search_client,
    search_request,
)

EXPERIMENTAL_TRACING = "GOOGLE_SDK_EXPERIMENTAL_PYTHON_TRACING_ENABLED"


@pytest.fixture()
def global_provider(monkeypatch):
    """An in-memory provider installed as the process-wide tracer provider.

    Any library span (Google's, gRPC's or ours) started through the global
    API lands here. The OTel globals are restored after the test.
    """
    from opentelemetry import trace
    from opentelemetry.util._once import Once

    exporter, provider = new_provider()
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    trace.set_tracer_provider(provider)
    assert trace.get_tracer_provider() is provider
    yield Traced(exporter, provider)


@pytest.fixture(params=[None, "true"], ids=["default", "experimental-tracing-env"])
def tracing_env(request, monkeypatch):
    if request.param is None:
        monkeypatch.delenv(EXPERIMENTAL_TRACING, raising=False)
    else:
        monkeypatch.setenv(EXPERIMENTAL_TRACING, request.param)
    return request.param


def _assert_vendor_methods_are_not_wrapped():
    from google.cloud.discoveryengine_v1 import (
        ConversationalSearchServiceAsyncClient,
        ConversationalSearchServiceClient,
        SearchServiceAsyncClient,
        SearchServiceClient,
    )

    for cls, name in (
        (SearchServiceClient, "search"),
        (SearchServiceClient, "search_lite"),
        (SearchServiceAsyncClient, "search"),
        (SearchServiceAsyncClient, "search_lite"),
        (ConversationalSearchServiceClient, "answer_query"),
        (ConversationalSearchServiceAsyncClient, "answer_query"),
    ):
        method = vars(cls)[name]
        # A plain function defined by the vendor module: nothing wrapped it.
        assert type(method).__name__ == "function", (cls.__name__, name, type(method))
        assert method.__module__.startswith("google.cloud.discoveryengine_v1."), method.__module__


def test_unwrapped_search_on_a_mock_transport_emits_no_span(global_provider, tracing_env):
    from google.cloud.discoveryengine_v1 import SearchResponse, SearchServiceClient
    from google.cloud.discoveryengine_v1.services.search_service.transports.grpc import (
        SearchServiceGrpcTransport,
    )

    _assert_vendor_methods_are_not_wrapped()
    # The channel is never connected: the stub call below is a unittest.mock.
    transport = SearchServiceGrpcTransport(channel=grpc.insecure_channel("127.0.0.1:9"))
    client = SearchServiceClient(transport=transport)
    tracer = global_provider.provider.get_tracer("control")

    with mock.patch.object(type(client.transport.search), "__call__") as call:
        call.return_value = SearchResponse(
            results=[SearchResponse.SearchResult(id="doc-{0}".format(i)) for i in range(SEARCH_RESULTS)],
            total_size=SEARCH_RESULTS,
        )
        with tracer.start_as_current_span("control"):
            pager = client.search(request=search_request())

    assert call.call_count == 1
    assert len(pager.results) == SEARCH_RESULTS
    # Only the control span: the client and google-api-core emitted none.
    assert global_provider.names() == ["control"]


def test_unwrapped_clients_on_the_real_grpc_transport_emit_no_span(global_provider, tracing_env):
    _assert_vendor_methods_are_not_wrapped()
    tracer = global_provider.provider.get_tracer("control")

    async def call_async(fake):
        search = async_search_client(fake)
        answer = async_answer_client(fake)
        try:
            pager = await search.search(request=search_request())
            await search.search_lite(request=search_request())
            await answer.answer_query(request=answer_request())
            return pager
        finally:
            await search.transport.close()
            await answer.transport.close()

    with FakeDiscoveryEngine() as fake:
        with tracer.start_as_current_span("control"):
            pager = search_client(fake).search(request=search_request())
            search_client(fake).search_lite(request=search_request())
            answer_client(fake).answer_query(request=answer_request())
            async_pager = asyncio.run(call_async(fake))

    assert fake.methods() == ["Search", "SearchLite", "AnswerQuery"] * 2
    assert len(pager.results) == SEARCH_RESULTS
    assert len(async_pager.results) == SEARCH_RESULTS
    assert global_provider.names() == ["control"]


def test_the_experimental_switch_state_is_part_of_the_measurement(tracing_env):
    # google-api-core turns its experimental path on only when the variable
    # is true and opentelemetry-instrumentation-grpc is importable. The
    # default test environment does not install the latter; the build
    # evidence also runs this file with it installed, where the switch is on
    # and the two tests above still count zero.
    observability = pytest.importorskip(
        "google.api_core._observability",
        reason="google-api-core without the experimental tracing path",
    )
    installed = importlib.util.find_spec("opentelemetry.instrumentation.grpc") is not None
    enabled = observability.is_otel_capabilities_enabled()
    assert enabled is (installed and tracing_env == "true")
