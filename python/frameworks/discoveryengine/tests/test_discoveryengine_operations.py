"""Offline v1 coverage through real GAPIC clients and synthetic transport responses."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from _discoveryengine_support import (
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    attrs,
    instrumented,
)
from google.auth.credentials import AnonymousCredentials
from google.cloud import discoveryengine_v1 as de
from opentelemetry.trace import StatusCode

CASES = [
    (
        "RankService",
        "rank",
        {
            "ranking_config": "projects/test/locations/global/rankingConfigs/default_ranking_config",
            "query": "query marker",
            "records": [{"id": "1", "content": "private record"}],
        },
        de.RankResponse(records=[{"id": "1", "content": "private record", "score": 0.9}]),
        "RERANKER",
    ),
    (
        "GroundedGenerationService",
        "check_grounding",
        {
            "grounding_config": "projects/test/locations/global/groundingConfigs/default_grounding_config",
            "answer_candidate": "query marker",
            "facts": [{"fact_text": "private fact"}],
        },
        de.CheckGroundingResponse(support_score=0.75, cited_facts=[{"chunk_text": "private fact"}]),
        "EVALUATOR",
    ),
    (
        "ConversationalSearchService",
        "converse_conversation",
        {
            "name": "projects/test/locations/global/dataStores/test/conversations/1",
            "query": {"input": "query marker"},
        },
        de.ConverseConversationResponse(
            reply={"summary": {"summary_text": "private reply"}}, search_results=[{"id": "1"}]
        ),
        "CHAIN",
    ),
]


def client_with_response(service, operation, response, is_async=False):
    cls = getattr(de, service + ("AsyncClient" if is_async else "Client"))
    client = cls(credentials=AnonymousCredentials())
    rpc = getattr(client.transport, operation)
    mock = AsyncMock(return_value=response) if is_async else Mock(return_value=response)
    client.transport._wrapped_methods[rpc] = mock
    return client, mock


@pytest.mark.parametrize("service,operation,payload,response,kind", CASES)
@pytest.mark.parametrize("is_async", [False, True])
def test_v1_semantic_operations(service, operation, payload, response, kind, is_async):
    async def run_async():
        client, mock = client_with_response(service, operation, response, True)
        try:
            result = await getattr(client, operation)(request=payload)
            assert result is response
            assert mock.call_count == 1
        finally:
            await client.transport.close()

    with instrumented() as traced:
        if is_async:
            asyncio.run(run_async())
        else:
            client, mock = client_with_response(service, operation, response)
            try:
                assert getattr(client, operation)(request=payload) is response
                assert mock.call_count == 1
            finally:
                client.transport.close()
    span = traced.one()
    assert span.name == "discoveryengine." + operation
    assert attrs(span)["gen_ai.span.kind"] == kind
    assert attrs(span)["input.value"] == "query marker"
    assert span.status.status_code is StatusCode.OK
    for marker in ("private record", "private fact", "private reply"):
        assert marker not in traced.wire()


def test_stream_lifetime_covers_consumption():
    with FakeDiscoveryEngine() as fake, instrumented() as traced:
        client = answer_client(fake)
        try:
            stream = client.stream_answer_query(request=answer_request())
            assert traced.spans() == []
            chunks = list(stream)
        finally:
            client.transport.close()
    span = traced.one()
    assert span.name == "discoveryengine.stream_answer_query"
    assert len(chunks) == 2
    assert attrs(span)["discoveryengine.answer.state"] == "SUCCEEDED"
    assert attrs(span)["discoveryengine.answer.length"] == len(chunks[-1].answer.answer_text)
    assert attrs(span)["discoveryengine.stream.chunk_count"] == 2
    assert span.status.status_code is StatusCode.OK


@pytest.mark.parametrize("service,operation,payload,response,kind", CASES)
def test_async_operation_cancellation(service, operation, payload, response, kind):
    async def run():
        client, mock = client_with_response(service, operation, response, True)
        mock.side_effect = asyncio.CancelledError()
        try:
            with pytest.raises(asyncio.CancelledError):
                await getattr(client, operation)(request=payload)
        finally:
            await client.transport.close()

    with instrumented() as traced:
        asyncio.run(run())
    span = traced.one()
    assert attrs(span)["discoveryengine.cancelled"] is True
    assert span.status.status_code is StatusCode.ERROR
    assert not span.events
