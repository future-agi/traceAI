"""Responses privacy and lifecycle contracts with real SDKs, no vendor traffic."""
import asyncio
import json

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor

KEY = "synthetic-parallel-vendor-key-DO-NOT-EXPORT"
QUESTION = "synthetic private question"

@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = register(project_name="recall-offline", project_type=ProjectType.OBSERVE,
                        span_exporter=exporter, batch=False, verbose=False)
    try:
        yield provider, exporter
    finally:
        provider.force_flush()
        provider.shutdown()


@pytest.fixture
def openai_tracing(tracing):
    provider, exporter = tracing
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield provider, exporter
    finally:
        instrumentor.uninstrument()


def sdk_client(handler, async_=False):
    http_client = (httpx.AsyncClient if async_ else httpx.Client)(
        transport=httpx.MockTransport(handler), trust_env=False)
    return (openai.AsyncOpenAI if async_ else openai.OpenAI)(
        api_key=KEY, base_url="https://api.parallel.ai/v1", max_retries=0, http_client=http_client)


def response_body(status="completed"):
    return {
        "id": "resp_synthetic", "object": "response", "created_at": 1, "model": "parallel",
        "status": status, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "output": [], "error": {"code": "server_error", "message": "synthetic upstream failed"} if status == "failed" else None,
        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
    }


def sse_event(kind, response):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, "sequence_number": 1, "response": response}) + "\n\n").encode()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_failed_response_must_not_export_success(openai_tracing, async_, stream):
    _, exporter = openai_tracing
    def handler(request):
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_event("response.failed", response_body("failed")))
        return httpx.Response(200, json=response_body("failed"))
    async def run():
        async with sdk_client(handler, True) as client:
            result = await client.responses.create(model="parallel", input=QUESTION, stream=stream)
            if stream:
                events = [event async for event in result]
                assert events[0].type == "response.failed"
            else:
                assert result.status == "failed"
    if async_:
        asyncio.run(run())
    else:
        with sdk_client(handler) as client:
            result = client.responses.create(model="parallel", input=QUESTION, stream=stream)
            if stream:
                events = list(result)  # Exhaustion is part of the lifecycle contract.
                assert events[0].type == "response.failed"
            else:
                assert result.status == "failed"
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["gen_ai.provider.name"] == "parallel"
    assert spans[0].status.status_code is StatusCode.ERROR, spans[0].to_json()


@pytest.mark.parametrize("async_", [False, True])
def test_early_stream_close_must_finish_span(openai_tracing, async_):
    provider, exporter = openai_tracing
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse_event("response.created", response_body("in_progress")) +
                                      sse_event("response.completed", response_body()))
    async def run():
        async with sdk_client(handler, True) as client:
            stream = await client.responses.create(model="parallel", input=QUESTION, stream=True)
            assert (await stream.__anext__()).type == "response.created"
            await stream.close()
            await stream.close()
            assert stream.response.is_closed
    if async_:
        asyncio.run(run())
    else:
        with sdk_client(handler) as client:
            stream = client.responses.create(model="parallel", input=QUESTION, stream=True)
            assert next(stream).type == "response.created"
            stream.close()
            stream.close()
            assert stream.response.is_closed
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"closed SDK stream exported {len(spans)} spans"
    assert spans[0].status.status_code is StatusCode.ERROR


@pytest.mark.parametrize("stage", ["request", "stream"])
def test_async_cancellation_must_finish_span(openai_tracing, stage):
    provider, exporter = openai_tracing
    async def run():
        waiting = asyncio.Event()
        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse_event("response.created", response_body("in_progress"))
                waiting.set()
                await asyncio.Event().wait()
        async def handler(request):
            if stage == "request":
                waiting.set()
                await asyncio.Event().wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=WaitingStream())
        async with sdk_client(handler, True) as client:
            stream = None
            async def call():
                nonlocal stream
                stream = await client.responses.create(model="parallel", input=QUESTION, stream=stage == "stream")
                if stage == "stream":
                    async for _ in stream:
                        pass
            task = asyncio.create_task(call())
            await asyncio.wait_for(waiting.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            if stream is not None:
                await stream.close()
    asyncio.run(run())
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"cancelled SDK {stage} exported {len(spans)} spans"
    assert spans[0].status.status_code is StatusCode.ERROR


@pytest.mark.parametrize("async_", [False, True])
def test_api_key_in_error_must_be_redacted(openai_tracing, async_):
    _, exporter = openai_tracing
    def handler(request):
        assert request.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "Invalid key: " + KEY}})
    async def run():
        async with sdk_client(handler, True) as client:
            await client.responses.create(model="parallel", input=QUESTION)
    with pytest.raises(openai.AuthenticationError) as caught:
        if async_:
            asyncio.run(run())
        else:
            with sdk_client(handler) as client:
                client.responses.create(model="parallel", input=QUESTION)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert KEY in str(caught.value)  # The caller still receives the original SDK exception.
    assert KEY not in spans[0].to_json(), "vendor credential exported in error status/event"


@pytest.mark.parametrize("async_", [False, True])
def test_hidden_instructions_not_reexported_in_output(tracing, async_):
    provider, exporter = tracing
    instruction = "synthetic confidential system instruction DO-NOT-EXPORT"
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True))
    def handler(request):
        assert json.loads(request.content)["instructions"] == instruction
        body = response_body()
        body["instructions"] = instruction
        body["output"] = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": "safe answer", "annotations": []}]}]
        return httpx.Response(200, json=body)
    async def run():
        async with sdk_client(handler, True) as client:
            response = await client.responses.create(model="parallel", input=QUESTION, instructions=instruction)
            assert response.instructions == instruction
    try:
        if async_:
            asyncio.run(run())
        else:
            with sdk_client(handler) as client:
                response = client.responses.create(model="parallel", input=QUESTION, instructions=instruction)
                assert response.instructions == instruction
        span, = exporter.get_finished_spans()
        assert span.attributes["input.value"] == "__REDACTED__"
        assert "safe answer" in span.attributes["output.value"]
        assert instruction not in span.to_json(), "FI_HIDE_INPUTS instruction leaked via output.value"
    finally:
        instrumentor.uninstrument()


@pytest.mark.parametrize("async_", [False, True])
def test_mcp_credentials_not_captured_as_tool_definitions(openai_tracing, async_):
    _, exporter = openai_tracing
    oauth = "synthetic-oauth-credential-DO-NOT-EXPORT"
    apikey = "synthetic-mcp-api-key-DO-NOT-EXPORT"
    tool = {"type": "mcp", "server_label": "synthetic", "server_url": "https://synthetic.invalid/mcp",
            "require_approval": "never", "authorization": oauth, "headers": {"X-Api-Key": apikey}}
    def handler(request):
        parsed = json.loads(request.content)
        assert parsed["tools"][0]["authorization"] == oauth
        assert parsed["tools"][0]["headers"]["X-Api-Key"] == apikey
        # Parallel explicitly scrubs tool credentials in its response.
        body = response_body()
        body["tools"] = [{**tool, "authorization": None, "headers": None}]
        return httpx.Response(200, json=body)
    async def run():
        async with sdk_client(handler, True) as client:
            await client.responses.create(model="parallel", input=QUESTION, tools=[tool])
    if async_:
        asyncio.run(run())
    else:
        with sdk_client(handler) as client:
            client.responses.create(model="parallel", input=QUESTION, tools=[tool])
    span, = exporter.get_finished_spans()
    assert oauth not in span.to_json(), "MCP OAuth token exported in gen_ai.tool.definitions"
    assert apikey not in span.to_json(), "MCP API key exported in gen_ai.tool.definitions"


@pytest.mark.parametrize("async_", [False, True])
def test_sdk_responses_stream_context_exit_finishes_span(openai_tracing, async_):
    provider, exporter = openai_tracing
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse_event("response.created", response_body("in_progress")) +
                                      sse_event("response.completed", response_body()))
    async def run():
        async with sdk_client(handler, True) as client, client.responses.stream(
            model="parallel", input=QUESTION
        ) as stream:
            async for event in stream:
                assert event.type == "response.created"
                break
    if async_:
        asyncio.run(run())
    else:
        with sdk_client(handler) as client, client.responses.stream(
            model="parallel", input=QUESTION
        ) as stream:
            for event in stream:
                assert event.type == "response.created"
                break
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"SDK context exit exported {len(spans)} spans"
    assert spans[0].status.status_code is StatusCode.ERROR



@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("mode", ["complete", "failed", "incomplete", "error", "eof", "empty"])
def test_terminal_status_and_close_are_consistent(openai_tracing, async_, mode):
    _, exporter = openai_tracing
    status = {"complete": "completed", "failed": "failed", "incomplete": "incomplete"}.get(mode)
    body = response_body(status or "in_progress")
    if mode == "failed":
        body["error"]["message"] = "upstream rejected " + KEY
    payload = sse_event("response.created", response_body("in_progress"))
    if status:
        payload += sse_event("response." + status, body)
    elif mode == "error":
        payload += ('data: ' + json.dumps({"type": "error", "code": "server_error",
                    "message": "upstream rejected " + KEY, "param": None,
                    "sequence_number": 2}) + '\n\n').encode()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    async def run():
        async with sdk_client(handler, True) as client:
            stream = await client.responses.create(model="parallel", input=QUESTION, stream=True)
            if mode != "empty":
                async for event in stream:
                    if status and event.type == "response." + status:
                        break  # Terminal event received, but no EOF read yet.
            await stream.close()
            await stream.close()
            assert stream.response.is_closed

    if async_:
        asyncio.run(run())
    else:
        with sdk_client(handler) as client:
            stream = client.responses.create(model="parallel", input=QUESTION, stream=True)
            if mode != "empty":
                for event in stream:
                    if status and event.type == "response." + status:
                        break
            stream.close()
            stream.close()
            assert stream.response.is_closed
    span, = exporter.get_finished_spans()
    assert span.status.status_code is (StatusCode.OK if mode == "complete" else StatusCode.ERROR)
    assert KEY not in span.to_json()
    if status:
        assert span.attributes["gen_ai.usage.input_tokens"] == 5


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_text", "hide_input_messages"])
def test_echoed_request_fields_never_become_output(tracing, async_, stream, flag):
    provider, exporter = tracing
    instruction = "synthetic-private-system-text"
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**{flag: True}))
    body = response_body()
    # Empty output exercises the accumulator's JSON fallback instead of output_text.
    body.update(instructions=instruction, metadata={"prompt": instruction},
                input=instruction, text={"format": {"type": "json_schema", "name": instruction}})

    def handler(request):
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_event("response.completed", body))
        return httpx.Response(200, json=body)

    async def run():
        async with sdk_client(handler, True) as client:
            result = await client.responses.create(model="parallel", input=QUESTION,
                                                   instructions=instruction, stream=stream)
            if stream:
                events = [event async for event in result]
                assert events[0].response.instructions == instruction
            else:
                assert result.instructions == instruction

    try:
        if async_:
            asyncio.run(run())
        else:
            with sdk_client(handler) as client:
                result = client.responses.create(model="parallel", input=QUESTION,
                                                  instructions=instruction, stream=stream)
                if stream:
                    events = list(result)
                    assert events[0].response.instructions == instruction
                else:
                    assert result.instructions == instruction
        span, = exporter.get_finished_spans()
        assert instruction not in span.to_json()
        output = json.loads(span.attributes["output.value"])
        assert output["status"] == "completed"
        assert output["usage"]["input_tokens"] == 5
    finally:
        instrumentor.uninstrument()


@pytest.mark.parametrize("async_", [False, True])
def test_raw_failed_response_retains_sdk_contract(openai_tracing, async_):
    _, exporter = openai_tracing

    def handler(request):
        return httpx.Response(200, json=response_body("failed"))

    async def run():
        async with sdk_client(handler, True) as client:
            raw = await client.responses.with_raw_response.create(model="parallel", input=QUESTION)
            assert raw.status_code == 200
            assert raw.parse().status == "failed"

    if async_:
        asyncio.run(run())
    else:
        with sdk_client(handler) as client:
            raw = client.responses.with_raw_response.create(model="parallel", input=QUESTION)
            assert raw.status_code == 200
            assert raw.parse().status == "failed"
    span, = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_hidden_error_details_apply_to_failed_response_json(tracing, async_, stream):
    provider, exporter = tracing
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_error_details=True))
    message = "synthetic-private-error-details"
    body = response_body("failed")
    body["error"]["message"] = message

    def handler(request):
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_event("response.failed", body))
        return httpx.Response(200, json=body)

    async def run():
        async with sdk_client(handler, True) as client:
            result = await client.responses.create(model="parallel", input=QUESTION, stream=stream)
            if stream:
                events = [event async for event in result]
                assert events[0].response.error.message == message
            else:
                assert result.error.message == message

    try:
        if async_:
            asyncio.run(run())
        else:
            with sdk_client(handler) as client:
                result = client.responses.create(model="parallel", input=QUESTION, stream=stream)
                if stream:
                    events = list(result)
                    assert events[0].response.error.message == message
                else:
                    assert result.error.message == message
        span, = exporter.get_finished_spans()
        assert span.status.status_code is StatusCode.ERROR
        assert message not in span.to_json()
    finally:
        instrumentor.uninstrument()


@pytest.mark.parametrize("async_", [False, True])
def test_mcp_wire_and_function_schema_are_not_mutated(openai_tracing, async_):
    _, exporter = openai_tracing
    token = 'synthetic-token-with-"quote"-and-\\slash'
    mcp = {"type": "mcp", "server_label": "synthetic", "server_url": "https://synthetic.invalid/mcp",
           "authorization": token, "headers": {"X-Custom-Credential": token}}
    function = {"type": "function", "name": "lookup", "parameters": {
        "type": "object", "properties": {"authorization": {"type": "string"}}}}
    tools = [mcp, function]
    original = json.loads(json.dumps(tools))

    def handler(request):
        assert json.loads(request.content)["tools"] == original
        body = response_body()
        body["tools"] = tools  # Don't rely on a provider scrubbing its response.
        return httpx.Response(200, json=body)

    async def run():
        async with sdk_client(handler, True) as client:
            result = await client.responses.create(model="parallel", input=QUESTION, tools=tools)
            assert result.tools[0].authorization == token

    if async_:
        asyncio.run(run())
    else:
        with sdk_client(handler) as client:
            result = client.responses.create(model="parallel", input=QUESTION, tools=tools)
            assert result.tools[0].authorization == token
    assert tools == original
    span, = exporter.get_finished_spans()
    assert "synthetic-token-with" not in span.to_json()
    definitions = json.loads(span.attributes["gen_ai.tool.definitions"])
    assert "authorization" not in definitions[0]
    assert "headers" not in definitions[0]
    assert definitions[1] == function
