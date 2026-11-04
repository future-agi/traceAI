"""Real-client HTTP/SSE attribution, including aliases and private/error requests."""

import asyncio
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_mistralai import MistralAIInstrumentor

NATIVE = int(version("mistralai").split(".")[0]) >= 3
Mistral = importlib.import_module("mistralai.client" if NATIVE else "mistralai").Mistral


@pytest.fixture(scope="module")
def server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if body["model"] == "error-alias":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"message":"synthetic-error-content"}')
                return
            response = {"id": "synthetic-id", "model": "resolved-model"}
            if self.path.endswith("embeddings"):
                response.update(object="list", data=[
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}
                ], usage={"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2})
            else:
                response.update(object="chat.completion", created=1, choices=[{
                    "index": 0, "finish_reason": "stop",
                    "delta" if body.get("stream") else "message": {
                        "role": "assistant", "content": "synthetic-output-content"
                    },
                }], usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
            self.end_headers()
            data = json.dumps(response)
            if body.get("stream"):
                data = f"data: {data}\n\ndata: [DONE]\n\n"
            self.wfile.write(data.encode())

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", requests
    finally:
        http.shutdown()
        worker.join()
        http.server_close()


@pytest.mark.parametrize("operation", ["complete", "stream", "embeddings"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("error", [False, True])
def test_request_and_response_attribution(server, operation, async_mode, private, error):
    endpoint, requests = server
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    config = TraceConfig(
        hide_inputs=private, hide_outputs=private,
        hide_llm_invocation_parameters=private,
    )
    requests_before = len(requests)
    model = "error-alias" if error else "requested-alias"
    params = {"model": model}
    if operation == "embeddings":
        params["inputs"] = ["synthetic-input-content"]
    else:
        params["messages"] = [{"role": "user", "content": "synthetic-input-content"}]

    # Supply and close transports explicitly, including SDK 1.1's clients.
    httpx = importlib.import_module("httpx2" if NATIVE else "httpx")

    async def run_async():
        async with httpx.AsyncClient() as transport:
            client = Mistral(api_key="synthetic-key", server_url=endpoint, async_client=transport)
            if operation == "stream":
                async with await client.chat.stream_async(**params) as stream:
                    assert len([chunk async for chunk in stream]) == 1
            elif operation == "embeddings":
                assert (await client.embeddings.create_async(**params)).model == "resolved-model"
            else:
                assert (await client.chat.complete_async(**params)).model == "resolved-model"

    def run_sync():
        with httpx.Client() as transport:
            client = Mistral(api_key="synthetic-key", server_url=endpoint, client=transport)
            if operation == "stream":
                with client.chat.stream(**params) as stream:
                    assert len(list(stream)) == 1
            elif operation == "embeddings":
                assert client.embeddings.create(**params).model == "resolved-model"
            else:
                assert client.chat.complete(**params).model == "resolved-model"

    try:
        def call():
            return asyncio.run(run_async()) if async_mode else run_sync()

        if error:
            # SDK 1.1 raises ResponseNotRead for HTTP stream errors; newer SDKs
            # raise SDKError. Instrumentation must preserve either behavior.
            with pytest.raises(Exception) as original:
                call()
            assert len(exporter.get_finished_spans()) == 0
        instrumentor.instrument(tracer_provider=provider, config=config)
        if error:
            with pytest.raises(type(original.value)) as traced:
                call()
            assert str(traced.value) == str(original.value)
        else:
            call()
        assert len(requests) == requests_before + (2 if error else 1)
        assert requests[-1]["model"] == model
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code == (StatusCode.ERROR if error else StatusCode.OK)
        attrs = dict(span.attributes)
        expected = {
            "gen_ai.provider.name": "mistral_ai" if NATIVE else "mistralai",
            "gen_ai.request.model": model,
            "gen_ai.response.model": None if error else "resolved-model",
        }
        assert {key: attrs.get(key) for key in expected} == expected
        if private:
            assert "gen_ai.request.parameters" not in attrs
            captured = str(attrs) + str(span.events) + str(span.status.description)
            for marker in ["synthetic-input-content", "synthetic-output-content", "synthetic-error-content"]:
                assert marker not in captured
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
