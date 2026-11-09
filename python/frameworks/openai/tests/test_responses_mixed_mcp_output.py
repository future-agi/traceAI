"""Mixed MCP/assistant Responses retain output through real SDKs and OTLP."""

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor

from .test_responses_privacy_and_raw_streams import KEY, body, client, sse

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from harness import Receiver, _flatten_attributes

ANSWER = "Mixed MCP assistant answer"
INSTRUCTION = "mixed-private-instruction"
MCP_AUTH = "synthetic-mixed-mcp-authorization"
MCP_HEADER = "synthetic-mixed-mcp-header"
TOOL_ERROR = "mixed-private-tool-error"
TOP_ERROR = "mixed-private-top-level-error"
TOOL = {
    "type": "mcp", "server_label": "synthetic",
    "server_url": "https://synthetic.invalid/mcp", "require_approval": "never",
    "authorization": MCP_AUTH, "headers": {"X-Synthetic-Secret": MCP_HEADER},
}


@pytest.fixture(scope="module")
def receiver():
    with Receiver() as receiver:
        yield receiver


def mixed_body(status="completed", answer_first=False):
    response = body(status)
    response["instructions"] = INSTRUCTION
    response["tools"] = [TOOL]
    response["error"] = {"code": "server_error", "message": TOP_ERROR}
    response["incomplete_details"] = {"reason": "max_output_tokens"}
    calls = [
        {
            "id": "mcp_mixed_" + str(i), "type": "mcp_call", "name": "lookup",
            "server_label": "synthetic", "arguments": json.dumps({"city": "city-" + str(i)}),
            "output": "tool-result-" + str(i) + " " + KEY + " " + MCP_AUTH + " " + MCP_HEADER,
            "error": {"type": "http_error", "code": 500, "message": TOOL_ERROR} if i else None,
        }
        for i in range(2)
    ]
    message = {
        "type": "message", "id": "msg_mixed", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": ANSWER, "annotations": []}],
    }
    response["output"] = [message, *calls] if answer_first else [*calls, message]
    return response


def collect(receiver, response, *, async_, stream=True, policy="default", early=False):
    receiver.clear()
    provider = TracerProvider()
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=receiver.endpoint)))
    config = TraceConfig(**({policy: True} if policy != "default" else {}))
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    seen = []

    def handler(request):
        assert request.url.host == "api.parallel.ai"
        params = json.loads(request.content)
        assert params["tools"] == [TOOL]
        assert params["instructions"] == INSTRUCTION
        seen.append(params)
        if stream:
            created = {**response, "status": "in_progress", "output": []}
            terminal = "response." + response["status"]
            if response["status"] == "cancelled":
                terminal = "response.completed"  # Body status must still prevent success.
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse("response.created", created) + sse(terminal, response))
        return httpx.Response(200, json=response)

    params = dict(model="parallel", input="question", instructions=INSTRUCTION, tools=[TOOL], stream=stream)

    def sync_call():
        with client(handler) as sdk:
            result = sdk.responses.create(**params)
            if stream:
                if early:
                    assert next(iter(result)).type == "response.created"
                    result.close()
                    result.close()
                    return None
                events = list(result)
                result.close()
                return events[-1].response
            return result

    async def async_call():
        async with client(handler, True) as sdk:
            result = await sdk.responses.create(**params)
            if stream:
                if early:
                    assert (await result.__aiter__().__anext__()).type == "response.created"
                    await result.close()
                    await result.close()
                    return None
                events = [event async for event in result]
                await result.close()
                return events[-1].response
            return result

    try:
        with provider.get_tracer("mixed-mcp-test").start_as_current_span("workflow") as parent:
            result = asyncio.run(async_call()) if async_ else sync_call()
        provider.force_flush()
        spans = memory.get_finished_spans()
        assert len(spans) == 2
        span = next(s for s in spans if s.name != "workflow")
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.attributes["gen_ai.provider.name"] == "parallel"
        assert span.attributes["gen_ai.request.model"] == "parallel"
        assert len(seen) == 1
        wire = receiver.spans()
        assert len(wire) == 2
        exported = next(s for s in wire if s["name"] != "workflow")
        assert base64.b64decode(exported["spanId"]).hex() == format(span.context.span_id, "016x")
        assert base64.b64decode(exported["parentSpanId"]).hex() == format(parent.get_span_context().span_id, "016x")
        attrs = _flatten_attributes(exported["attributes"])
        if early:
            assert "output.value" not in attrs
        else:
            assert attrs["output.value"] == span.attributes["output.value"]
        return result, span, exported, config
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("answer_first", [False, True])
@pytest.mark.parametrize("policy", ["default", "hide_inputs", "hide_outputs", "hide_error_details"])
def test_mixed_output_retains_distinct_calls_and_privacy(receiver, async_, stream, answer_first, policy):
    response = mixed_body(answer_first=answer_first)
    result, span, exported, config = collect(receiver, response, async_=async_, stream=stream, policy=policy)
    # Neither the wire request nor the SDK response is sanitized for the application.
    assert result.instructions == INSTRUCTION
    calls = [item for item in result.output if item.type == "mcp_call"]
    assert [item.id for item in calls] == ["mcp_mixed_0", "mcp_mixed_1"]
    for item in calls:
        assert KEY in item.output and MCP_AUTH in item.output and MCP_HEADER in item.output
    assert TOOL_ERROR in result.model_dump_json()
    assert span.status.status_code is StatusCode.OK
    assert exported["status"]["code"] == "STATUS_CODE_OK"
    wire = json.dumps(exported)
    for secret in (KEY, MCP_AUTH, MCP_HEADER, TOP_ERROR):
        assert secret not in wire
    if policy == "hide_inputs":
        assert INSTRUCTION not in wire
    if policy == "hide_outputs":
        assert span.attributes["output.value"] == "__REDACTED__"
        for text in (ANSWER, "tool-result-", "city-", "mcp_mixed_", TOOL_ERROR):
            assert text not in wire
        return
    output = json.loads(span.attributes["output.value"])
    assert next(iter(output)) == "output_text"
    assert output["output_text"] == ANSWER  # Preserve the answer-first preview.
    assert span.attributes["output.mime_type"] == "application/json"
    for request_field in ("instructions", "input", "tools", "error"):
        assert request_field not in output
    captured = [item for item in output["output"] if item["type"] == "mcp_call"]
    assert [item["id"] for item in captured] == ["mcp_mixed_0", "mcp_mixed_1"]
    for i, item in enumerate(captured):
        assert item["name"] == "lookup"
        assert json.loads(item["arguments"]) == {"city": "city-" + str(i)}
        assert item["output"].startswith("tool-result-" + str(i))
        assert item["server_label"] == "synthetic"
    assert (TOOL_ERROR not in wire) is config.hide_error_details
    if config.hide_error_details:
        assert captured[1]["error"] == {"type": "http_error", "code": 500}
    else:
        assert captured[1]["error"]["message"] == TOOL_ERROR


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("kind", ["mcp_list_tools", "mcp_approval_request", "mcp_approval_response"])
@pytest.mark.parametrize("hide_errors", [False, True])
def test_mixed_mcp_metadata_items_survive(receiver, async_, kind, hide_errors):
    response = mixed_body()
    item = {"id": "mcp_metadata", "type": kind}
    if kind == "mcp_list_tools":
        item.update(server_label="synthetic", error=TOOL_ERROR,
                    tools=[{"name": "lookup", "input_schema": {"type": "object"}}])
    elif kind == "mcp_approval_request":
        item.update(server_label="synthetic", name="lookup", arguments='{"city":"city-0"}')
    else:
        item.update(approval_request_id="mcp_approval", approve=True, reason="approved in fixture")
    response["output"] = [item, response["output"][-1]]
    result, span, exported, _ = collect(
        receiver, response, async_=async_, policy="hide_error_details" if hide_errors else "default"
    )
    assert result.output[0].id == "mcp_metadata"
    captured = json.loads(span.attributes["output.value"])["output"][0]
    expected = dict(item)
    if kind == "mcp_list_tools" and hide_errors:
        expected.pop("error")
        assert TOOL_ERROR not in json.dumps(exported)
        assert TOOL_ERROR in result.model_dump_json()
    assert captured == expected
    assert span.status.status_code is StatusCode.OK


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
def test_mixed_output_does_not_turn_non_success_into_success(receiver, async_, status):
    result, span, exported, _ = collect(receiver, mixed_body(status), async_=async_, policy="hide_error_details")
    assert result.status == status
    assert span.status.status_code is StatusCode.ERROR
    assert exported["status"]["code"] == "STATUS_CODE_ERROR"
    output = json.loads(span.attributes["output.value"])
    assert output["status"] == status
    assert "tool-result-0" in span.attributes["output.value"]
    assert TOOL_ERROR not in json.dumps(exported)


@pytest.mark.parametrize("async_", [False, True])
def test_text_only_stream_keeps_plain_answer(receiver, async_):
    response = mixed_body()
    response["output"] = response["output"][-1:]
    _, span, _, _ = collect(receiver, response, async_=async_)
    assert span.attributes["output.value"] == ANSWER
    assert span.status.status_code is StatusCode.OK


@pytest.mark.parametrize("async_", [False, True])
def test_interrupted_mixed_stream_finishes_once_without_inventing_output(receiver, async_):
    result, span, exported, _ = collect(receiver, mixed_body(), async_=async_, early=True)
    assert result is None
    assert span.status.status_code is StatusCode.ERROR
    assert exported["status"]["code"] == "STATUS_CODE_ERROR"
    assert "tool-result-" not in json.dumps(exported)
