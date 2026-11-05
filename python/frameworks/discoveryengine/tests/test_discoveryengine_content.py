"""Content controls and actual OTLP/HTTP round trips with synthetic credentials."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from _discoveryengine_support import (
    ANSWER_TEXT,
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    attrs,
    instrumented,
    search_client,
    search_request,
)
from fi_instrumentation import TraceConfig
from google.api_core.exceptions import PermissionDenied
from google.cloud import discoveryengine_v1 as de
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.trace import StatusCode
from test_discoveryengine_operations import CASES, client_with_response
from test_discoveryengine_streaming import chunk, mock_client

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from harness import Receiver, _flatten_attributes, post_otlp  # noqa: E402


@pytest.mark.parametrize(
    "capture,hide_inputs,hide_outputs,visible",
    [
        (False, False, False, False),
        (True, False, False, True),
        (True, True, False, False),
        (True, False, True, False),
    ],
)
def test_otlp_capture_is_opt_in_and_hide_flags_win(capture, hide_inputs, hide_outputs, visible):
    config = TraceConfig(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    with (
        FakeDiscoveryEngine() as fake,
        instrumented(config=config, capture_content=capture) as traced,
    ):
        search = search_client(fake)
        answer = answer_client(fake)
        try:
            search.search(request=search_request())
            answer.answer_query(request=answer_request())
            list(answer.stream_answer_query(request=answer_request()))
        finally:
            search.transport.close()
            answer.transport.close()
        for service, operation, payload, response, _ in CASES:
            client, _ = client_with_response(service, operation, response)
            try:
                getattr(client, operation)(request=payload)
            finally:
                client.transport.close()
    # Real OTLP protobuf serialization, HTTP collector reception and decoding.
    payload = MessageToDict(encode_spans(traced.spans()))
    with Receiver() as receiver:
        assert post_otlp(payload, receiver.endpoint) == 200
        spans = receiver.spans()
    assert len(spans) == 6
    wire = json.dumps(spans)
    for marker in (ANSWER_TEXT, "private record", "private fact", "private reply"):
        assert (marker in wire) is visible
    for span in spans:
        values = _flatten_attributes(span["attributes"])
        assert ("output.value" in values) is visible
        if visible:
            json.loads(values["output.value"])
    if hide_inputs:
        assert "query marker" not in wire


def test_content_is_bounded_valid_json_and_credentials_and_pii_are_scrubbed():
    token = "synthetic-secret-marker"
    record = {"id": "1", "title": "person@example.invalid " + token, "content": "long " * 5000}
    response = de.RankResponse(records=[record] * 100)
    with instrumented(capture_content=True, config=TraceConfig(pii_redaction=True)) as traced:
        client, _ = client_with_response("RankService", "rank", response)
        try:
            client.rank(
                request={
                    "query": "question",
                    "ranking_config": "projects/test/locations/global/rankingConfigs/default",
                },
                metadata=(("x-goog-api-key", token),),
            )
        finally:
            client.transport.close()
    output = attrs(traced.one())["output.value"]
    assert len(output.encode()) <= 16 * 1024
    parsed = json.loads(output)
    assert len(parsed["records"]) <= 20
    assert token not in output
    assert "person@example.invalid" not in output
    assert "<EMAIL_ADDRESS>" in output
    assert "[redacted]" in output


def test_stream_deltas_are_not_captured_and_terminal_snapshot_is_not_duplicated():
    token = "synthetic-secret-marker"
    with instrumented(capture_content=True) as traced:
        client, _ = mock_client(
            iter(
                [
                    chunk("synthetic-"),
                    chunk("secret-marker"),
                    chunk(token + " safe answer", de.Answer.State.SUCCEEDED),
                ]
            )
        )
        try:
            list(
                client.stream_answer_query(
                    request=answer_request(), metadata=(("x-goog-api-key", token),)
                )
            )
        finally:
            client.transport.close()
    output = attrs(traced.one())["output.value"]
    assert token not in output
    assert "synthetic-" not in output
    assert json.loads(output)["answer_text"] == "[redacted] safe answer"


@pytest.mark.parametrize("service,operation,payload,response,kind", CASES)
@pytest.mark.parametrize("is_async", [False, True])
def test_new_operations_hide_arbitrary_echoed_payloads_and_preserve_errors(
    service, operation, payload, response, kind, is_async
):
    error = PermissionDenied("private fact; private record; private history; query marker")

    async def run():
        client, mock = client_with_response(service, operation, response, True)
        mock.side_effect = error
        try:
            with pytest.raises(PermissionDenied) as raised:
                await getattr(client, operation)(request=payload)
            assert raised.value is error
        finally:
            await client.transport.close()

    with instrumented(config=TraceConfig(hide_inputs=True, hide_outputs=True)) as traced:
        if is_async:
            asyncio.run(run())
        else:
            client, mock = client_with_response(service, operation, response)
            mock.side_effect = error
            try:
                with pytest.raises(PermissionDenied) as raised:
                    getattr(client, operation)(request=payload)
                assert raised.value is error
            finally:
                client.transport.close()
    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    assert attrs(span)["discoveryengine.error.status"] == "PERMISSION_DENIED"
    wire = encode_spans(traced.spans()).SerializeToString()
    for marker in (b"private fact", b"private record", b"private history", b"query marker"):
        assert marker not in wire


def test_flattened_conversation_query_and_grounding_metrics():
    service, operation, payload, response, _ = CASES[2]
    with instrumented() as traced:
        client, _ = client_with_response(service, operation, response)
        try:
            client.converse_conversation(name=payload["name"], query=payload["query"])
        finally:
            client.transport.close()
        service, operation, payload, response, _ = CASES[1]
        client, _ = client_with_response(service, operation, response)
        try:
            client.check_grounding(payload)
        finally:
            client.transport.close()
    conversation, grounding = traced.spans()
    assert attrs(conversation)["input.value"] == "query marker"
    assert attrs(grounding)["discoveryengine.grounding.support_score"] == 0.75
    assert attrs(grounding)["discoveryengine.result_count"] == 1


def test_bad_capture_option_does_not_patch_clients():
    from traceai_discoveryengine import DiscoveryEngineInstrumentor

    original = de.RankServiceClient.rank
    with pytest.raises(TypeError, match="capture_content"):
        DiscoveryEngineInstrumentor().instrument(capture_content="false")
    assert de.RankServiceClient.rank is original
