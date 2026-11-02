"""Real SDK, synthetic provider transport, and localhost OTLP migration contract."""

import asyncio
import copy
import json
import sys
import threading
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

DeepSeekInstrumentor = pytest.importorskip("traceai_deepseek").DeepSeekInstrumentor

try:
    from openai._base_client import httpx2 as httpx
except ImportError:
    from openai._base_client import httpx

MODERN = tuple(map(int, openai.__version__.split(".")[:2])) >= (1, 69)
if MODERN:
    from traceai_openai import OpenAIInstrumentor

BASE = dict(id="completion-offline", created=1, model="deepseek-reasoner")
REASONING = "PRIVATE_REASONING_7382"
ANSWER = "PRIVATE_ANSWER_1294"
TOOL_ARGS = '{"secret":"PRIVATE_TOOL_9271"}'
USAGE = dict(
    prompt_tokens=5,
    completion_tokens=7,
    total_tokens=12,
    prompt_cache_hit_tokens=3,
    prompt_cache_miss_tokens=2,
    completion_tokens_details=dict(reasoning_tokens=4),
)
TOOL = dict(
    id="call-offline",
    type="function",
    function=dict(name="lookup", arguments=TOOL_ARGS),
)
MESSAGE = dict(
    role="assistant", content=ANSWER, reasoning_content=REASONING, tool_calls=[TOOL]
)
BODY = dict(
    BASE,
    object="chat.completion",
    usage=USAGE,
    choices=[dict(index=0, message=MESSAGE, finish_reason="tool_calls")],
)
RESPONSE = dict(
    id="resp-offline",
    object="response",
    created_at=1,
    status="completed",
    model="deepseek-reasoner",
    output=[
        dict(
            id="msg-offline",
            type="message",
            status="completed",
            role="assistant",
            content=[dict(type="output_text", text=ANSWER, annotations=[])],
        )
    ],
    usage=dict(
        input_tokens=5,
        output_tokens=7,
        total_tokens=12,
        input_tokens_details=dict(cached_tokens=3),
        output_tokens_details=dict(reasoning_tokens=4),
    ),
)


def respond(request):
    payload = json.loads(request.content)
    if request.url.path.endswith("/responses"):
        if payload.get("stream"):
            event = dict(
                type="response.completed", sequence_number=1, response=RESPONSE
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="data: " + json.dumps(event) + "\n\n",
            )
        return httpx.Response(200, json=RESPONSE)
    if payload.get("stream"):
        deltas = [
            dict(role="assistant", reasoning_content=REASONING[:10]),
            dict(
                reasoning_content=REASONING[10:],
                content=ANSWER,
                tool_calls=[
                    dict(
                        TOOL,
                        index=0,
                        function=dict(name="lookup", arguments=TOOL_ARGS[:12]),
                    )
                ],
            ),
            dict(tool_calls=[dict(index=0, function=dict(arguments=TOOL_ARGS[12:]))]),
        ]
        chunks = [
            dict(
                BASE,
                object="chat.completion.chunk",
                usage=None,
                choices=[dict(index=0, delta=d, finish_reason=None)],
            )
            for d in deltas
        ]
        chunks.append(
            dict(
                BASE,
                object="chat.completion.chunk",
                usage=USAGE,
                choices=[dict(index=0, delta={}, finish_reason="tool_calls")],
            )
        )
        data = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=data + "data: [DONE]\n\n",
        )
    return httpx.Response(200, json=BODY)


@pytest.fixture
def wire(monkeypatch):
    for field in fields(TraceConfig):
        monkeypatch.delenv(field.metadata["env_var"], raising=False)
    captured = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            decoded = ExportTraceServiceRequest.FromString(payload)
            captured.extend(
                span
                for resource in decoded.resource_spans
                for scope in resource.scope_spans
                for span in scope.spans
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", timeout=5
            )
        )
    )
    instruments = []

    def start(route, **privacy):
        if route != "legacy" and not MODERN:
            pytest.skip("Shared OpenAI instrumentor requires SDK >=1.69")
        classes = dict(legacy=[DeepSeekInstrumentor])
        if MODERN:
            classes.update(
                shared=[OpenAIInstrumentor],
                legacy_first=[DeepSeekInstrumentor, OpenAIInstrumentor],
                shared_first=[OpenAIInstrumentor, DeepSeekInstrumentor],
            )
        for cls in classes[route]:
            inst = cls()
            inst.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
            instruments.append(inst)
        return captured

    yield start
    for inst in reversed(instruments):
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
    provider.shutdown()
    server.shutdown()
    thread.join()
    server.server_close()


def attrs(spans):
    assert len(spans) == 1
    return {
        a.key: getattr(a.value, a.value.WhichOneof("value"))
        for a in spans[0].attributes
    }


def call(
    asynchronous=False,
    streaming=False,
    mode="full",
    host="api.deepseek.com",
    responses=False,
):
    params = dict(model="deepseek-reasoner", stream=streaming)
    params.update(
        dict(input="PRIVATE_INPUT_7134")
        if responses
        else dict(messages=[dict(role="user", content="PRIVATE_INPUT_7134")])
    )

    async def run():
        async with openai.AsyncOpenAI(
            api_key="offline-placeholder",
            base_url=f"https://{host}/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            resource = client.responses if responses else client.chat.completions
            response = await resource.create(**params)
            if streaming:
                if mode == "close":
                    await response.close()
                else:
                    async with response as entered:
                        assert entered is response
                        if mode != "empty":
                            async for _chunk in response:
                                if mode == "early":
                                    break
                assert response.response.is_closed
            return response

    if asynchronous:
        return asyncio.run(run())
    with openai.OpenAI(
        api_key="offline-placeholder",
        base_url=f"https://{host}/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        resource = client.responses if responses else client.chat.completions
        response = resource.create(**params)
        if streaming:
            if mode == "close":
                response.close()
            else:
                with response as entered:
                    assert entered is response
                    if mode != "empty":
                        for _chunk in response:
                            if mode == "early":
                                break
            assert response.response.is_closed
        return response


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_chat_migration_parity(wire, route, asynchronous, streaming):
    spans = wire(route)
    response = call(asynchronous, streaming)
    a = attrs(spans)
    assert a["gen_ai.provider.name"] == "deepseek"
    assert a["gen_ai.request.model"] == "deepseek-reasoner"
    assert a["deepseek.prompt_cache_hit_tokens"] == 3
    assert a["deepseek.prompt_cache_miss_tokens"] == 2
    assert a["deepseek.reasoning_tokens"] == 4
    assert a["gen_ai.usage.output_tokens"] == 7
    assert a["deepseek.reasoning_content"] == REASONING
    assert a["deepseek.output_reasoning"] == REASONING
    assert a["gen_ai.output.messages.0.reasoning_content"] == REASONING
    assert a["deepseek.finish_reason"] == "tool_calls"
    assert a["deepseek.tool_calls_count"] == 1
    assert a["deepseek.tool_calls.0.function.arguments"] == TOOL_ARGS
    if not streaming:
        assert response.choices[0].message.reasoning_content == REASONING


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["early", "empty", "close"])
def test_stream_lifecycle(wire, route, asynchronous, mode):
    spans = wire(route)
    call(asynchronous, streaming=True, mode=mode)
    assert attrs(spans)["gen_ai.provider.name"] == "deepseek"


@pytest.mark.parametrize("route", ["shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_responses(wire, route, asynchronous, streaming):
    spans = wire(route)
    call(asynchronous, streaming, responses=True)
    assert attrs(spans)["gen_ai.provider.name"] == "deepseek"
    assert attrs(spans)["gen_ai.usage.output_tokens"] == 7


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "policy", ["hide_outputs", "hide_output_messages", "hide_output_text"]
)
def test_output_privacy_on_wire(wire, route, asynchronous, streaming, policy):
    spans = wire(route, **{policy: True})
    call(asynchronous, streaming)
    a = attrs(spans)
    assert a["deepseek.prompt_cache_hit_tokens"] == 3
    assert a["deepseek.reasoning_tokens"] == 4
    if policy != "hide_outputs":
        a.pop("output.value", None)  # Message/text policies do not hide raw output.
    for value in [REASONING, ANSWER, "PRIVATE_TOOL_9271"]:
        assert value not in json.dumps(a)


@pytest.mark.parametrize(
    "host",
    [
        "api.deepseek.com.evil.example",
        "proxy.example/api.deepseek.com",
        "notdeepseek.com",
    ],
)
def test_legacy_exact_host(wire, host):
    spans = wire("legacy")
    call(host=host)
    assert not spans


@pytest.mark.parametrize("route", ["legacy_first", "shared_first"])
@pytest.mark.parametrize("remove", ["legacy", "shared"])
def test_independent_teardown(wire, route, remove):
    spans = wire(route)
    (
        DeepSeekInstrumentor() if remove == "legacy" else OpenAIInstrumentor()
    ).uninstrument()
    call()
    assert attrs(spans)["deepseek.reasoning_content"] == REASONING


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_api_error_privacy(wire, monkeypatch, route, asynchronous):
    spans = wire(route, hide_error_details=True)

    def fail(request):
        return httpx.Response(
            400,
            json=dict(
                error=dict(message="PRIVATE_ERROR_1842", type="invalid_request_error")
            ),
        )

    monkeypatch.setattr(__import__(__name__, fromlist=["respond"]), "respond", fail)
    with pytest.raises(openai.BadRequestError, match="PRIVATE_ERROR_1842"):
        call(asynchronous)
    assert attrs(spans)["gen_ai.request.model"] == "deepseek-reasoner"
    assert spans[0].status.code == 2
    assert b"PRIVATE_ERROR_1842" not in spans[0].SerializeToString()


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
def test_idempotent_instrumentation(wire, route):
    spans = wire(route)
    DeepSeekInstrumentor().instrument()
    call()
    assert attrs(spans)["gen_ai.provider.name"] == "deepseek"
    # The fixture owns legacy teardown only when it started that instrumentor.
    if route == "shared":
        DeepSeekInstrumentor().uninstrument()


@pytest.mark.parametrize("route", ["legacy_first", "shared_first"])
def test_shared_configuration_owns_request(wire, route):
    spans = wire(route)
    DeepSeekInstrumentor().uninstrument()
    DeepSeekInstrumentor().instrument(config=TraceConfig(hide_outputs=True))
    call()
    assert attrs(spans)["deepseek.reasoning_content"] == REASONING


def test_legacy_does_not_defer_to_shared_without_extension_support(wire, monkeypatch):
    spans = wire("legacy_first")
    monkeypatch.setattr(OpenAIInstrumentor, "_supports_deepseek_extensions", False)
    call()
    assert len(spans) == 2  # Existing behavior with an older shared package.
    legacy = [span for span in spans if span.name == "deepseek.chat.completions"]
    assert attrs(legacy)["deepseek.reasoning_content"] == REASONING


def test_shared_non_deepseek_has_no_aliases(wire):
    spans = wire("shared")
    call(host="api.openai.com")
    assert attrs(spans)["gen_ai.provider.name"] == "openai"
    assert not any(key.startswith("deepseek.") for key in attrs(spans))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_explicit_configuration_overrides_environment(wire, monkeypatch, asynchronous):
    spans = wire("shared", hide_outputs=False)
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    call(asynchronous, streaming=True)
    assert attrs(spans)["deepseek.reasoning_content"] == REASONING


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("explicit_close", [False, True])
def test_cancelled_stream_is_finished_as_error(wire, route, explicit_close):
    spans = wire(route, hide_error_details=True)

    async def run():
        reading = asyncio.Event()
        cancellations = []

        class WaitingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                reading.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    cancellations.append(error)
                    raise
                yield b""

        def respond(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=WaitingBody()
            )

        async with openai.AsyncOpenAI(
            api_key="offline-key",
            base_url="https://api.deepseek.com",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            stream = await client.chat.completions.create(
                model="deepseek-reasoner",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
            )

            async def consume():
                try:
                    async for _ in stream:
                        pass
                except asyncio.CancelledError as error:
                    cancellations.append(error)
                    raise
                finally:
                    if explicit_close:
                        await stream.close()

            task = asyncio.create_task(consume())
            await asyncio.wait_for(reading.wait(), timeout=5)
            task.cancel("PRIVATE_CANCEL_REASON")
            with pytest.raises(asyncio.CancelledError):
                await task
            # Python 3.10 may lose the cancellation message at the Task boundary.
            # Assert identity where the exception passes through instrumentation.
            assert len(cancellations) == 2
            assert cancellations[0] is cancellations[1]
            assert len(spans) == 1, "cancelled requests must export exactly one span"
            assert spans[0].status.code == 2
            assert any(event.name == "exception" for event in spans[0].events)
            assert b"PRIVATE_CANCEL_REASON" not in spans[0].SerializeToString()
            await stream.close()
            assert len(spans) == 1
            assert spans[0].status.code == 2

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "policy", [None, "hide_outputs", "hide_output_messages", "hide_output_text"]
)
def test_responses_tool_output_privacy(wire, monkeypatch, asynchronous, streaming, policy):
    spans = wire("shared", **({policy: True} if policy else {}))
    body = copy.deepcopy(RESPONSE)
    body["output"] = [
        dict(
            type="function_call", id="fc-offline", call_id="call-offline",
            name="lookup", arguments=TOOL_ARGS, status="completed",
        )
    ]
    monkeypatch.setattr(sys.modules[__name__], "RESPONSE", body)
    call(asynchronous=asynchronous, streaming=streaming, responses=True)
    a = attrs(spans)
    assert a["gen_ai.usage.output_tokens"] == 7
    if not policy:
        arguments = "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"
        assert a[arguments] == TOOL_ARGS
        return
    if policy != "hide_outputs":
        a.pop("output.value", None)  # Raw output has its own documented hide policy.
    assert "PRIVATE_TOOL_9271" not in json.dumps(a)


@pytest.mark.parametrize(
    "event_type", ["response.completed", "response.incomplete", "response.failed"]
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("has_output", [False, True])
@pytest.mark.parametrize(
    "streaming,mode", [(True, "full"), (True, "early"), (False, "full")]
)
def test_responses_terminal_events(
    wire, monkeypatch, event_type, asynchronous, has_output, streaming, mode
):
    spans = wire("shared", hide_error_details=True)
    body = copy.deepcopy(RESPONSE)
    body["status"] = event_type.split(".")[1]
    if not has_output:
        body["output"] = []
    if event_type == "response.incomplete":
        body["incomplete_details"] = {"reason": "max_output_tokens"}
    if event_type == "response.failed":
        body["error"] = {"code": "server_error", "message": "PRIVATE_VENDOR_ERROR"}

    def respond(request):
        if not streaming:
            return httpx.Response(200, json=body)
        event = dict(type=event_type, sequence_number=1, response=body)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content="event: " + event_type + "\ndata: " + json.dumps(event) + "\n\n",
        )

    monkeypatch.setattr(sys.modules[__name__], "respond", respond)
    response = call(
        asynchronous=asynchronous, streaming=streaming, responses=True, mode=mode
    )
    if not streaming:
        assert response.status == body["status"]
        if event_type == "response.failed":
            assert response.error.message == "PRIVATE_VENDOR_ERROR"
    a = attrs(spans)
    assert a["gen_ai.usage.input_tokens"] == 5
    assert a["gen_ai.usage.output_tokens"] == 7
    assert a["gen_ai.usage.total_tokens"] == 12
    assert a["deepseek.reasoning_tokens"] == 4
    assert a["deepseek.response_id"] == "resp-offline"
    if has_output:
        if streaming:
            assert a["output.value"] == ANSWER
        assert a["gen_ai.output.messages.0.message.content"] == ANSWER
    assert spans[0].status.code == (2 if event_type == "response.failed" else 1)
    assert b"PRIVATE_VENDOR_ERROR" not in spans[0].SerializeToString()


@pytest.mark.parametrize("route", ["legacy", "shared", "legacy_first", "shared_first"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("hide_error_details", [False, True])
def test_cancelled_create_is_finished_as_error(
    wire, route, streaming, responses, hide_error_details
):
    spans = wire(route, hide_error_details=hide_error_details)
    if responses and route == "legacy":
        pytest.skip("Legacy instrumentor supports Chat Completions only")

    async def run():
        waiting = asyncio.Event()
        cancellations = []

        async def respond(request):
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                cancellations.append(error)
                raise

        async with openai.AsyncOpenAI(
            api_key="offline-key",
            base_url="https://api.deepseek.com",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            async def create():
                resource = client.responses if responses else client.chat.completions
                params = dict(model="deepseek-reasoner", stream=streaming)
                params.update(
                    dict(input="hello") if responses
                    else dict(messages=[dict(role="user", content="hello")])
                )
                try:
                    await resource.create(**params)
                except asyncio.CancelledError as error:
                    cancellations.append(error)
                    raise

            task = asyncio.create_task(create())
            await asyncio.wait_for(waiting.wait(), timeout=5)
            task.cancel("PRIVATE_CREATE_CANCEL")
            with pytest.raises(asyncio.CancelledError):
                await task
            # Compare before the Task boundary, which can discard details on 3.10.
            assert len(cancellations) == 2
            assert cancellations[0] is cancellations[1]
            assert len(spans) == 1, "cancelled create must finish before client close"
            assert spans[0].status.code == 2
            assert sum(event.name == "exception" for event in spans[0].events) == 1
            exported = spans[0].SerializeToString()
            assert (b"PRIVATE_CREATE_CANCEL" not in exported) == hide_error_details
        assert len(spans) == 1
        assert spans[0].status.code == 2

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("has_summary", [False, True])
@pytest.mark.parametrize(
    "policy", [None, "hide_outputs", "hide_output_messages", "hide_output_text"]
)
def test_responses_plaintext_reasoning(
    wire, monkeypatch, asynchronous, streaming, has_summary, policy
):
    spans = wire("shared", **({policy: True} if policy else {}))
    body = copy.deepcopy(RESPONSE)
    summary = "PRIVATE_SUMMARY_7382"
    body["output"].insert(0, dict(
        id="rs-offline", type="reasoning", status="completed",
        summary=[dict(type="summary_text", text=summary)] if has_summary else [],
        content=[dict(type="reasoning_text", text=text) for text in (REASONING, "more")],
    ))
    monkeypatch.setattr(sys.modules[__name__], "RESPONSE", body)
    response = call(asynchronous=asynchronous, streaming=streaming, responses=True)
    if not streaming:
        # Instrumentation must not strip reasoning from the SDK object.
        assert response.model_dump()["output"][0]["content"][0]["text"] == REASONING
    a = attrs(spans)
    assert a["gen_ai.usage.output_tokens"] == 7
    assert spans[0].status.code == 1
    if policy != "hide_outputs":
        if streaming:
            assert a["output.value"] == ANSWER
        else:
            assert json.loads(a["output.value"])["output_text"] == ANSWER
    if policy:
        if policy != "hide_outputs":
            a.pop("output.value", None)  # Raw output has an independent policy.
        encoded = json.dumps(a)
        assert REASONING not in encoded
        assert summary not in encoded
        assert ANSWER not in encoded
    else:
        prefix = "gen_ai.output.messages.0.message.contents."
        offset = int(has_summary)
        if has_summary:
            assert a[f"{prefix}0.message_content.text"] == summary
        assert a[f"{prefix}{offset}.message_content.text"] == REASONING
        assert a[f"{prefix}{offset + 1}.message_content.text"] == "more"
        assert a["gen_ai.output.messages.1.message.content"] == ANSWER
