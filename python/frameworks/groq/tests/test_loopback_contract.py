"""Real SDK and OTLP HTTP acceptance tests using only loopback synthetic data."""

import asyncio
import json
import os
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest
from fi_instrumentation import TraceConfig
from google.protobuf.json_format import MessageToDict
from groq import AsyncGroq, Groq
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from traceai_groq import GroqInstrumentor

PROMPT = "synthetic-groq-input-contract-marker"
ANSWER = "synthetic-groq-output-contract-marker"
MODEL = "synthetic-contract-model"
COMPLETION = {
    "id": "synthetic-completion", "object": "chat.completion", "created": 0,
    "model": MODEL,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}
FLAGS = ["FI_HIDE_INPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_INPUT_TEXT",
         "FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_OUTPUT_TEXT"]


@pytest.fixture
def endpoint(request, monkeypatch):
    for flag in FLAGS + ["FI_HIDE_ERROR_DETAILS"]:
        monkeypatch.delenv(flag, raising=False)
    requests, exports = [], []
    state = {"malformed": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/traces":
                message = ExportTraceServiceRequest.FromString(body)
                exports.append(MessageToDict(message, preserving_proto_field_name=True))
                payload, content_type = b"", "application/x-protobuf"
            else:
                assert self.path == "/openai/v1/chat/completions"
                params = json.loads(body)
                requests.append(params)
                content_type = "application/json"
                if state["malformed"]:
                    payload = b'{"choices":'
                elif params.get("stream"):
                    chunk = dict(COMPLETION, object="chat.completion.chunk", choices=[{
                        "index": 0, "delta": {"role": "assistant", "content": ANSWER},
                        "finish_reason": "stop",
                    }])
                    payload = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(COMPLETION).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    state.update(url=f"http://127.0.0.1:{server.server_port}", requests=requests, exports=exports)
    try:
        yield state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        # Optional qualification evidence: the complete received OTLP record,
        # including resource attributes, events and status, not just span attributes.
        if path := os.environ.get("GROQ_CONTRACT_EVIDENCE"):
            with open(path, "a", encoding="utf-8") as output:
                output.write(json.dumps({"test": request.node.nodeid,
                                         "requests": requests, "otlp": exports}) + "\n")


@contextmanager
def traced(endpoint):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
        endpoint=endpoint["url"] + "/v1/traces", timeout=5,
    )))
    instrumentor = GroqInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    try:
        yield
    finally:
        provider.force_flush()
        instrumentor.uninstrument()
        provider.shutdown()


@contextmanager
def raw_sync(client, api, params):
    resource = getattr(client.chat.completions, api)
    if api == "with_streaming_response":
        with resource.create(**params) as response:
            yield response
    else:
        response = resource.create(**params)
        try:
            yield response
        finally:
            response.close()


@asynccontextmanager
async def raw_async(client, api, params):
    resource = getattr(client.chat.completions, api)
    if api == "with_streaming_response":
        async with resource.create(**params) as response:
            yield response
    else:
        response = await resource.create(**params)
        try:
            yield response
        finally:
            await response.close()


def invoke(endpoint, asynchronous, api="direct", stream=False, method="parse"):
    params = {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}], "stream": stream}
    kwargs = {"api_key": "synthetic-not-a-key", "base_url": endpoint["url"],
              "max_retries": 0}

    def consume_sync(response):
        if method == "prefetch_error":
            assert response.read() == b'{"choices":'
            with pytest.raises(ValueError):
                response.parse()
            return
        if method.startswith("iter_"):
            return list(getattr(response, method)())
        result = response.parse()
        return list(result) if stream else result

    async def consume_async(response):
        if method == "prefetch_error":
            assert await response.read() == b'{"choices":'
            with pytest.raises(ValueError):
                await response.parse()
            return
        if method.startswith("iter_"):
            return [part async for part in getattr(response, method)()]
        result = await response.parse()
        return [chunk async for chunk in result] if stream else result

    async def run_async():
        async with AsyncGroq(http_client=httpx.AsyncClient(trust_env=False), **kwargs) as client:
            if api == "direct":
                result = await client.chat.completions.create(**params)
                return [chunk async for chunk in result] if stream else result
            async with raw_async(client, api, params) as response:
                return await consume_async(response)

    if asynchronous:
        return asyncio.run(run_async())
    with Groq(http_client=httpx.Client(trust_env=False), **kwargs) as client:
        if api == "direct":
            result = client.chat.completions.create(**params)
            return list(result) if stream else result
        with raw_sync(client, api, params) as response:
            return consume_sync(response)


def one_span(endpoint):
    spans = [span for export in endpoint["exports"]
             for resource in export["resource_spans"]
             for scope in resource["scope_spans"] for span in scope["spans"]]
    assert len(spans) == 1
    return spans[0]


def attributes(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("api", ["direct", "with_raw_response", "with_streaming_response"])
def test_privacy_settings_on_received_otlp(endpoint, monkeypatch, flag, hidden, asynchronous, stream, api):
    monkeypatch.setenv(flag, str(hidden).lower())
    with traced(endpoint):
        result = invoke(endpoint, asynchronous, api, stream)
    assert endpoint["requests"][0]["messages"][0]["content"] == PROMPT
    assert (result[0].choices[0].delta.content if stream else result.choices[0].message.content) == ANSWER
    span = one_span(endpoint)
    attrs = attributes(span)
    serialized = json.dumps(endpoint["exports"])
    marker = PROMPT if "INPUT" in flag else ANSWER
    aggregate = "input.value" if "INPUT" in flag else "output.value"
    if hidden:
        assert marker not in serialized
        assert attrs[aggregate] == "__REDACTED__"
    else:
        assert marker in attrs[aggregate]
    assert (ANSWER if "INPUT" in flag else PROMPT) in serialized
    assert attrs["gen_ai.request.model"] == MODEL
    assert attrs["gen_ai.response.model"] == MODEL
    assert int(attrs["gen_ai.usage.total_tokens"]) == 5
    assert span["status"]["code"] == "STATUS_CODE_OK"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("api", ["with_raw_response", "with_streaming_response"])
@pytest.mark.parametrize("method", ["iter_bytes", "iter_text", "iter_lines"])
def test_raw_json_iterators_preserve_completion(endpoint, asynchronous, api, method):
    with traced(endpoint):
        parts = invoke(endpoint, asynchronous, api, method=method)
    body = b"".join(parts).decode() if method == "iter_bytes" else "".join(parts)
    assert json.loads(body) == COMPLETION
    span = one_span(endpoint)
    attrs = attributes(span)
    assert attrs["output.value"] == ANSWER
    assert attrs["gen_ai.response.model"] == MODEL
    assert int(attrs["gen_ai.usage.input_tokens"]) == 3
    assert int(attrs["gen_ai.usage.output_tokens"]) == 2
    assert int(attrs["gen_ai.usage.total_tokens"]) == 5
    assert span["status"]["code"] == "STATUS_CODE_OK"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("api", ["with_raw_response", "with_streaming_response"])
def test_prefetched_malformed_json_records_error(endpoint, asynchronous, api):
    endpoint["malformed"] = True
    with traced(endpoint):
        invoke(endpoint, asynchronous, api, method="prefetch_error")
    span = one_span(endpoint)
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert [event["name"] for event in span["events"]] == ["exception"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unspecified_timeout_is_not_a_request_parameter(endpoint, asynchronous):
    with traced(endpoint):
        invoke(endpoint, asynchronous)
    params = json.loads(attributes(one_span(endpoint))["gen_ai.request.parameters"])
    assert "timeout" not in params
