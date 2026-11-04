"""Trace context: the span is current while the transport sends, nests, and carries using_* attributes."""

from __future__ import annotations

import asyncio
import json

import grpc
import pytest
from opentelemetry import trace as trace_api

from _discoveryengine_support import (
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    async_answer_client,
    async_search_client,
    attrs,
    instrumented,
    search_client,
    search_request,
)


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


def _current_span_id() -> int:
    return trace_api.get_current_span().get_span_context().span_id


class _RecordCurrentSpan(grpc.UnaryUnaryClientInterceptor):
    """Runs where a gRPC client instrumentation would start its span."""

    def __init__(self, seen):
        self.seen = seen

    def intercept_unary_unary(self, continuation, client_call_details, request):
        self.seen.append(_current_span_id())
        return continuation(client_call_details, request)


class _AsyncRecordCurrentSpan(grpc.aio.UnaryUnaryClientInterceptor):
    def __init__(self, seen):
        self.seen = seen

    async def intercept_unary_unary(self, continuation, client_call_details, request):
        self.seen.append(_current_span_id())
        return await continuation(client_call_details, request)


def test_sync_rpc_runs_inside_the_discoveryengine_span(fake):
    from google.cloud.discoveryengine_v1 import SearchServiceClient
    from google.cloud.discoveryengine_v1.services.search_service.transports.grpc import (
        SearchServiceGrpcTransport,
    )

    seen = []
    channel = grpc.intercept_channel(grpc.insecure_channel(fake.target), _RecordCurrentSpan(seen))
    client = SearchServiceClient(transport=SearchServiceGrpcTransport(channel=channel))
    with instrumented() as traced:
        client.search(request=search_request())
        client.search_lite(request=search_request())

    assert seen == [span.context.span_id for span in traced.spans()]
    assert len(seen) == 2
    assert _current_span_id() == 0


def test_async_rpc_runs_inside_the_discoveryengine_span(fake):
    from google.cloud.discoveryengine_v1 import ConversationalSearchServiceAsyncClient
    from google.cloud.discoveryengine_v1.services.conversational_search_service.transports.grpc_asyncio import (
        ConversationalSearchServiceGrpcAsyncIOTransport,
    )

    seen = []

    async def call():
        channel = grpc.aio.insecure_channel(fake.target, interceptors=[_AsyncRecordCurrentSpan(seen)])
        client = ConversationalSearchServiceAsyncClient(
            transport=ConversationalSearchServiceGrpcAsyncIOTransport(channel=channel)
        )
        try:
            await client.answer_query(request=answer_request())
        finally:
            await client.transport.close()

    with instrumented() as traced:
        asyncio.run(call())

    assert seen == [traced.one().context.span_id]


def test_spans_are_children_of_the_active_span(fake):
    async def call():
        client = async_search_client(fake)
        try:
            await client.search(request=search_request())
        finally:
            await client.transport.close()

    with instrumented() as traced:
        tracer = traced.provider.get_tracer("agent")
        with tracer.start_as_current_span("agent.step") as parent:
            search_client(fake).search(request=search_request())
            answer_client(fake).answer_query(request=answer_request())
            asyncio.run(call())

    spans = traced.spans()
    agent = [span for span in spans if span.name == "agent.step"][0]
    assert agent.context.span_id == parent.get_span_context().span_id
    children = [span for span in spans if span.name != "agent.step"]
    assert [span.name for span in children] == [
        "discoveryengine.search",
        "discoveryengine.answer_query",
        "discoveryengine.search",
    ]
    for span in children:
        assert span.parent.span_id == agent.context.span_id
        assert span.context.trace_id == agent.context.trace_id


def test_a_call_without_an_active_span_is_a_root_span(fake):
    with instrumented() as traced:
        search_client(fake).search(request=search_request())

    assert traced.one().parent is None


def test_using_session_and_using_user_stamp_sync_and_async_spans(fake):
    from fi_instrumentation import using_session, using_user

    async def call():
        search = async_search_client(fake)
        answer = async_answer_client(fake)
        try:
            await search.search(request=search_request())
            await answer.answer_query(request=answer_request())
        finally:
            await search.transport.close()
            await answer.transport.close()

    with instrumented() as traced:
        with using_session("s1"), using_user("u1"):
            search_client(fake).search_lite(request=search_request())
            answer_client(fake).answer_query(request=answer_request())
            asyncio.run(call())
        search_client(fake).search(request=search_request())

    spans = traced.spans()
    assert len(spans) == 5
    for span in spans[:4]:
        values = attrs(span)
        assert values["session.id"] == "s1", span.name
        assert values["user.id"] == "u1", span.name
    assert "session.id" not in attrs(spans[4])
    assert "user.id" not in attrs(spans[4])


def test_using_attributes_stamps_metadata_and_tags(fake):
    from fi_instrumentation import using_attributes

    metadata = {"team": "search", "run": 3}
    with instrumented() as traced:
        with using_attributes(session_id="s2", user_id="u2", metadata=metadata, tags=["t1", "t2"]):
            answer_client(fake).answer_query(request=answer_request())

    values = attrs(traced.one())
    assert values["session.id"] == "s2"
    assert values["user.id"] == "u2"
    assert json.loads(values["metadata"]) == metadata
    assert list(values["tag.tags"]) == ["t1", "t2"]
    # The Discovery Engine session is a separate attribute and is not set here.
    assert "discoveryengine.session" not in values


def test_spans_come_from_fitracer_not_a_plain_tracer(fake, monkeypatch):
    # Mutation guard: if the wrapper used the plain OTel tracer, the using_*
    # stamps and the PII pass on attributes would silently disappear.
    from fi_instrumentation import FITracer
    from fi_instrumentation import using_session

    created = []
    original = FITracer.start_span

    def spy(self, *args, **kwargs):
        span = original(self, *args, **kwargs)
        created.append(span)
        return span

    monkeypatch.setattr(FITracer, "start_span", spy)
    with instrumented() as traced:
        with using_session("s3"):
            search_client(fake).search(request=search_request())

    assert len(created) == 1
    assert attrs(traced.one())["session.id"] == "s3"
