"""Offline provider contracts using real SDKs and a localhost OTLP receiver."""

import asyncio
import gc
import json
import threading
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest
import requests
import traceai_openai
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_openai import OpenAIInstrumentor
from traceai_openai._providers import provider_from_host

# OpenAI 3.x uses httpx2; never substitute a different transport implementation.
if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx

MODEL = "local-model"
PRIVATE_INPUT = "private-input-sentinel"
PRIVATE_OUTPUT = "private-output-sentinel"
FAKE_KEY = "synthetic-key-not-a-credential"


@pytest.fixture(scope="module")
def collector():
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, bodies
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def wire(collector):
    port, bodies = collector
    bodies.clear()
    session = requests.Session()
    session.trust_env = False
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{port}/v1/traces",
                session=session,
            )
        )
    )
    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(
            hide_inputs=True,
            hide_outputs=True,
            hide_embedding_vectors=True,
            hide_llm_invocation_parameters=True,
        ),
    )
    yield bodies
    instrumentor.uninstrument()
    provider.shutdown()
    session.close()


def spans_on_wire(bodies):
    return [
        span
        for body in bodies
        for resource in ExportTraceServiceRequest.FromString(body).resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]


def attributes(span):
    return {entry.key: entry.value.string_value for entry in span.attributes}


def set_provider(client, provider):
    # Attribute lookup lets the unchanged dev revision demonstrate the missing API.
    traceai_openai.set_client_provider(client, provider=provider)


def handler_for(operation):
    def handler(request):
        # Provider attribution must stay out of vendor request headers and bodies.
        assert not any(
            "traceai" in name or "provider" in name for name in request.headers
        )
        assert "provider" not in json.loads(request.content)
        if operation.startswith("error-"):
            return httpx.Response(401, json={"error": {"message": "Unauthorized"}})
        if operation == "embeddings":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": MODEL,
                    "data": [
                        {"object": "embedding", "index": 0, "embedding": [0.25, -0.5]}
                    ],
                    "usage": {"prompt_tokens": 2, "total_tokens": 2},
                },
            )
        if operation.startswith("responses"):
            response = {
                "id": "r1",
                "object": "response",
                "created_at": 1,
                "model": MODEL,
                "status": "completed",
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
                "output": [
                    {
                        "id": "m1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": PRIVATE_OUTPUT,
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            }
            if operation == "responses":
                return httpx.Response(200, json=response)
            events = [
                {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": {**response, "status": "in_progress", "output": []},
                },
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "item_id": "m1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": PRIVATE_OUTPUT,
                },
                {
                    "type": "response.completed",
                    "sequence_number": 2,
                    "response": response,
                },
            ]
            body = "".join(
                f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
            )
        elif operation == "chat-stream":
            event = {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": PRIVATE_OUTPUT},
                        "finish_reason": "stop",
                    }
                ],
            }
            body = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n"
        else:
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": PRIVATE_OUTPUT},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                },
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    return handler


def make_client(base_url, operation="chat", use_async=False):
    cls = openai.AsyncOpenAI if use_async else openai.OpenAI
    http_cls = httpx.AsyncClient if use_async else httpx.Client
    return cls(
        api_key=FAKE_KEY,
        base_url=base_url,
        max_retries=0,
        http_client=http_cls(
            transport=httpx.MockTransport(handler_for(operation)), trust_env=False
        ),
    )


def call(client, operation):
    if "embeddings" in operation:
        return client.embeddings.create(
            model=MODEL, input=PRIVATE_INPUT, encoding_format="float"
        )
    if "responses" in operation:
        return client.responses.create(
            model=MODEL, input=PRIVATE_INPUT, stream=operation.endswith("-stream")
        )
    return client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PRIVATE_INPUT}],
        stream=operation.endswith("-stream"),
    )


def assert_response(response, operation):
    if operation == "embeddings":
        assert response.data[0].embedding == [0.25, -0.5]
    elif operation == "responses":
        assert response.output_text == PRIVATE_OUTPUT
    else:
        assert response.choices[0].message.content == PRIVATE_OUTPUT


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "operation",
    [
        "chat",
        "chat-stream",
        "embeddings",
        "responses",
        "responses-stream",
        "error-chat",
        "error-embeddings",
        "error-responses",
    ],
)
@pytest.mark.parametrize(
    "base_url,override,expected",
    [
        ("https://ollama.com/v1", None, "ollama"),
        ("http://localhost:11434/v1", "ollama", "ollama"),
        ("http://127.0.0.1:11434/v1", "ollama", "ollama"),
        ("http://localhost:11434/v1", None, "openai"),
        ("https://ollama.com.example/v1", None, "openai"),
    ],
    ids=[
        "official",
        "localhost-override",
        "loopback-override",
        "unknown-local",
        "lookalike",
    ],
)
def test_provider_on_wire(wire, use_async, operation, base_url, override, expected):
    async def run_async():
        async with make_client(base_url, operation, use_async=True) as client:
            if override:
                set_provider(client, override)
            response = await call(client, operation)
            if operation.endswith("-stream"):
                assert not spans_on_wire(wire)
                chunks = [chunk async for chunk in response]
                assert chunks
            else:
                assert_response(response, operation)

    def run_sync():
        with make_client(base_url, operation) as client:
            if override:
                set_provider(client, override)
            response = call(client, operation)
            if operation.endswith("-stream"):
                assert not spans_on_wire(wire)
                assert list(response)
            else:
                assert_response(response, operation)

    def run():
        return asyncio.run(run_async()) if use_async else run_sync()

    if operation.startswith("error-"):
        with pytest.raises(openai.AuthenticationError):
            run()
    else:
        run()
    spans = spans_on_wire(wire)
    assert len(spans) == 1
    attrs = attributes(spans[0])
    assert attrs["gen_ai.provider.name"] == expected
    assert attrs["gen_ai.request.model"] == MODEL
    assert spans[0].status.code == (2 if operation.startswith("error-") else 1)
    assert spans[0].end_time_unix_nano >= spans[0].start_time_unix_nano
    payload = b"".join(wire)
    for sentinel in (PRIVATE_INPUT, PRIVATE_OUTPUT, FAKE_KEY):
        assert sentinel.encode() not in payload


@pytest.mark.parametrize(
    "host,expected",
    [
        ("OLLAMA.COM.", "ollama"),
        ("ollama.com", "ollama"),
        ("localhost", "openai"),
        ("127.0.0.1", "openai"),
        ("::1", "openai"),
        ("api.ollama.com", "openai"),
        ("notollama.com", "openai"),
        ("ollama.com.evil.example", "openai"),
        (None, "openai"),
    ],
)
def test_exact_official_host_only(host, expected):
    assert provider_from_host(host) == expected


def test_override_precedence_clear_and_client_isolation(wire):
    with (
        make_client("https://api.openai.com/v1") as a,
        make_client("https://api.openai.com/v1") as b,
    ):
        set_provider(a, "ollama")
        call(a, "chat")
        call(b, "chat")
        set_provider(a, "vllm")
        call(a, "chat")
        set_provider(a, None)
        call(a, "chat")
    assert [attributes(s)["gen_ai.provider.name"] for s in spans_on_wire(wire)] == [
        "ollama",
        "openai",
        "vllm",
        "openai",
    ]


def test_async_clients_do_not_share_overrides(wire):
    async def run():
        async with (
            make_client("http://localhost:11434/v1", use_async=True) as a,
            make_client("http://localhost:11434/v1", use_async=True) as b,
        ):
            set_provider(a, "ollama")
            set_provider(b, "vllm")
            await asyncio.gather(call(a, "chat"), call(b, "chat"))

    asyncio.run(run())
    assert sorted(
        attributes(s)["gen_ai.provider.name"] for s in spans_on_wire(wire)
    ) == [
        "ollama",
        "vllm",
    ]


def test_copy_requires_explicit_override(wire):
    with make_client("http://localhost:11434/v1") as original:
        set_provider(original, "ollama")
        copied = original.with_options()
        call(copied, "chat")
        set_provider(copied, "ollama")
        call(copied, "chat")
    assert [attributes(s)["gen_ai.provider.name"] for s in spans_on_wire(wire)] == [
        "openai",
        "ollama",
    ]


@pytest.mark.parametrize(
    "invalid", ["", "Ollama", " ollama", "https://ollama.com", "x" * 65, 123]
)
def test_invalid_override_does_not_replace_existing_value(wire, invalid):
    with make_client("http://localhost:11434/v1") as client:
        set_provider(client, "ollama")
        with pytest.raises(ValueError, match="provider"):
            set_provider(client, invalid)
        call(client, "chat")
    assert attributes(spans_on_wire(wire)[0])["gen_ai.provider.name"] == "ollama"


def test_override_requires_an_openai_client():
    with pytest.raises(TypeError, match="client"):
        set_provider(object(), "ollama")


@pytest.mark.parametrize("provider", ["o", "x" * 64, "self_hosted.v1-2"])
def test_valid_identifier_boundaries(wire, provider):
    with make_client("http://localhost:11434/v1") as client:
        set_provider(client, None)  # Clearing an absent override is safe.
        set_provider(client, provider)
        set_provider(client, provider)  # Repeated configuration is idempotent.
        call(client, "chat")
    assert attributes(spans_on_wire(wire)[0])["gen_ai.provider.name"] == provider


def test_unhashable_client_subclass_keeps_existing_tracing(wire):
    class Client(openai.OpenAI):
        def __eq__(self, other):
            return self is other

    with Client(
        base_url="http://localhost:11434/v1",
        api_key=FAKE_KEY,
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler_for("chat")),
            trust_env=False,
        ),
    ) as client:
        call(client, "chat")
        set_provider(client, "ollama")
        call(client, "chat")
    assert [attributes(s)["gen_ai.provider.name"] for s in spans_on_wire(wire)] == [
        "openai",
        "ollama",
    ]


def test_override_does_not_keep_client_alive():
    client = make_client("http://localhost:11434/v1")
    set_provider(client, "ollama")
    ref = weakref.ref(client)
    client.close()
    del client
    gc.collect()
    assert ref() is None
