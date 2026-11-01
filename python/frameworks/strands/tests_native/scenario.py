"""Real Strands Agent/OTLP scenarios, each run in an isolated interpreter."""

import argparse
import asyncio
import base64
import gzip
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import HTTPSpanExporter
from google.protobuf.json_format import MessageToJson, Parse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands import Agent, tool
from strands.models.model import Model
from traceai_strands import (
    StrandsInstrumentor,
    StrandsSpanExporter,
    configure_strands_tracing,
)

parser = argparse.ArgumentParser()
parser.add_argument("--wire", choices=["protobuf", "json"], default="protobuf")
parser.add_argument("--gzip", action="store_true")
parser.add_argument("--capture", action="store_true")
parser.add_argument("--hide", default="")
parser.add_argument("--batch", action="store_true")
parser.add_argument("--error", action="store_true")
parser.add_argument("--tool-error", action="store_true")
parser.add_argument("--long-tool-error", action="store_true")
parser.add_argument("--readme", action="store_true")
parser.add_argument("--async-stream", action="store_true")
args = parser.parse_args()
received = []


def convert_ids(value, to_wire):
    # OTLP JSON differs from generic protobuf JSON: IDs are hex, not base64.
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"traceId", "spanId", "parentSpanId"}:
                value[key] = (
                    base64.b64decode(item).hex()
                    if to_wire
                    else base64.b64encode(bytes.fromhex(item)).decode()
                )
            else:
                convert_ids(item, to_wire)
    elif isinstance(value, list):
        for item in value:
            convert_ids(item, to_wire)
    return value


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/v1/chat/completions":
            # Real Strands OpenAIModel + OpenAI SDK, with only the HTTP transport
            # replaced by a loopback SSE endpoint. The README runs verbatim.
            data = json.loads(body)
            assert data["model"] == "gpt-4o-mini"
            chunks = [
                {
                    "id": "offline",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": data["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "Paris"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "offline",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": data["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                {
                    "id": "offline",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": data["model"],
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                    },
                },
            ]
            payload = (
                "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                + "data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload.encode())))
            self.end_headers()
            self.wfile.write(payload.encode())
            return
        assert self.path == "/tracer/v1/traces", self.path
        assert self.headers["X-Api-Key"] == "synthetic-api"
        assert self.headers["X-Secret-Key"] == "synthetic-secret"
        compressed = self.headers.get("Content-Encoding") == "gzip"
        if compressed:
            body = gzip.decompress(body)
        request = ExportTraceServiceRequest()
        if self.headers.get("Content-Type") == "application/json":
            Parse(json.dumps(convert_ids(json.loads(body), False)), request)
        else:
            request.ParseFromString(body)
        received.append((request, compressed))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
os.environ.update(
    FI_BASE_URL=base,
    FI_API_KEY="synthetic-api",
    FI_SECRET_KEY="synthetic-secret",
    OPENAI_API_KEY="synthetic-openai",
    OPENAI_BASE_URL=base + "/v1",
)


class JSONExporter(SpanExporter):
    """Test-only OTLP JSON transport: exercises the same projected ReadableSpans."""

    def export(self, spans):
        body = json.dumps(
            convert_ids(json.loads(MessageToJson(encode_spans(spans))), True)
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": "synthetic-api",
            "X-Secret-Key": "synthetic-secret",
        }
        if args.gzip:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        with urlopen(
            Request(base + "/tracer/v1/traces", body, headers), timeout=5
        ) as response:
            assert response.status == 200
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis=30000):
        return True

    def shutdown(self):
        pass


class OfflineModel(Model):
    def __init__(self):
        self.config = {"model_id": "synthetic-model"}

    def update_config(self, **kwargs):
        self.config = dict(self.config, **kwargs)

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield

    async def stream(self, messages, *unused, **kwargs):
        if args.error:
            raise RuntimeError("ERROR-SENTINEL")
        yield {"messageStart": {"role": "assistant"}}
        if messages[-1]["role"] != "user" or not any(
            "toolResult" in b for b in messages[-1]["content"]
        ):
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tool-1", "name": "echo"}},
                    "contentBlockIndex": 0,
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": '{"text":"INPUT-SENTINEL"}'}},
                    "contentBlockIndex": 0,
                }
            }
            stop = "tool_use"
        else:
            yield {
                "contentBlockDelta": {
                    "delta": {"text": "OUTPUT-SENTINEL"},
                    "contentBlockIndex": 0,
                }
            }
            stop = "end_turn"
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": stop}}
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": 3,
                    "outputTokens": 4,
                    "totalTokens": 7,
                    "cacheReadInputTokens": 1,
                    "cacheWriteInputTokens": 2,
                },
                "metrics": {"latencyMs": 1},
            }
        }


@tool
def echo(text: str) -> str:
    """Echo synthetic text."""
    if args.tool_error:
        raise RuntimeError("ERROR-SENTINEL" + ("x" * 4096 if args.long_tool_error else ""))
    return text


if args.readme:
    readme = Path(__file__).resolve().parents[1] / "README.md"
    code = readme.read_text().split("```python\n", 1)[1].split("```", 1)[0]
    exec(compile(code, str(readme), "exec"), {})  # noqa: S102 - test the checked-in README verbatim
    spans = [
        s
        for req, _ in received
        for rs in req.resource_spans
        for ss in rs.scope_spans
        for s in ss.spans
    ]
    assert len(spans) == 3, len(spans)
    assert (
        sum(
            next(
                (
                    int(a.value.int_value)
                    for a in s.attributes
                    if a.key == "gen_ai.usage.total_tokens"
                ),
                0,
            )
            for s in spans
        )
        == 7
    )
else:
    transport = (
        JSONExporter()
        if args.wire == "json"
        else HTTPSpanExporter(
            compression=Compression.Gzip if args.gzip else Compression.NoCompression,
        )
    )
    config = TraceConfig(**{flag: True for flag in args.hide.split(",") if flag})
    adapter = StrandsSpanExporter(
        transport, capture_content=args.capture, config=config
    )
    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="offline-strands",
        set_global_tracer_provider=True,
        batch=args.batch,
        span_exporter=adapter,
        verbose=False,
    )
    raw = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(raw))
    # Even ambient OTLP settings must not create a raw/privacy-bypassing copy.
    os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = base + "/tracer/v1/traces"
    before = dict(os.environ)
    from strands.telemetry import StrandsTelemetry

    with (
        patch.object(
            StrandsTelemetry,
            "setup_otlp_exporter",
            side_effect=AssertionError("extra exporter"),
        ),
        patch.object(
            StrandsTelemetry,
            "setup_meter",
            side_effect=AssertionError("metrics exporter"),
        ),
    ):
        handle = configure_strands_tracing(tracer_provider=provider)
        assert configure_strands_tracing(tracer_provider=provider) is handle
        assert StrandsInstrumentor()._tracer_provider is provider
    assert dict(os.environ) == before
    agent = Agent(
        model=OfflineModel(),
        tools=[echo],
        callback_handler=None,
        trace_attributes={
            "session.id": "synthetic-session",
            "custom.content": "PRIVATE-SENTINEL",
        },
    )
    try:
        if args.async_stream:

            async def run():
                async for _ in agent.stream_async("INPUT-SENTINEL"):
                    pass

            asyncio.run(run())
        else:
            agent("INPUT-SENTINEL")
            agent("INPUT-SENTINEL second turn")
    except RuntimeError:
        assert args.error
    provider.force_flush()
    originals = raw.get_finished_spans()
    if args.long_tool_error:
        # Prove the real SDK truncated the serialized error, not merely that a
        # short/well-formed tool result was removed by the existing redactor.
        truncated = [
            value
            for span in originals
            for attrs in [span.attributes, *(event.attributes for event in span.events)]
            for value in (attrs or {}).values()
            if isinstance(value, str) and len(value) == 512 and "ERROR-SENTINEL" in value
        ]
        assert truncated, "SDK truncation was not exercised"
        for value in truncated:
            try:
                json.loads(value)
            except ValueError:
                break
        else:
            raise AssertionError("Expected truncated, undecodable native JSON")
    spans = [
        s
        for req, _ in received
        for rs in req.resource_spans
        for ss in rs.scope_spans
        for s in ss.spans
    ]
    assert len(spans) == len(originals), (len(spans), len(originals))
    assert len({s.span_id for s in spans}) == len(spans)
    assert {s.span_id.hex() for s in spans} == {
        format(s.context.span_id, "016x") for s in originals
    }
    original_by_id = {format(s.context.span_id, "016x"): s for s in originals}
    for span in spans:
        original = original_by_id[span.span_id.hex()]
        assert span.trace_id.hex() == format(original.context.trace_id, "032x")
        assert int.from_bytes(span.parent_span_id, "big") == (
            original.parent.span_id if original.parent else 0
        )
        assert span.start_time_unix_nano == original.start_time
        assert span.end_time_unix_nano == original.end_time
        attributes = {a.key: a.value for a in span.attributes}
        assert attributes["gen_ai.span.kind"].string_value in {
            "AGENT",
            "CHAIN",
            "LLM",
            "TOOL",
        }
        assert "custom.content" not in attributes
        if args.tool_error and attributes["gen_ai.span.kind"].string_value == "TOOL":
            assert span.status.code == 2
        if attributes["gen_ai.span.kind"].string_value != "LLM":
            assert not any(key.startswith("gen_ai.usage.") for key in attributes)
        for direction in ("input", "output"):
            hidden = any(
                getattr(config, flag)
                for flag in (
                    (
                        "hide_inputs",
                        "hide_input_messages",
                        "hide_input_text",
                        "hide_input_images",
                    )
                    if direction == "input"
                    else ("hide_outputs", "hide_output_messages", "hide_output_text")
                )
            )
            if not args.capture or hidden:
                assert direction + ".value" not in attributes
                assert "gen_ai." + direction + ".messages" not in attributes
        assert not any(event.name.startswith("gen_ai.") for event in span.events)
        if args.error and (not args.capture or config.hide_error_details):
            assert not span.status.message
            assert "ERROR-SENTINEL" not in MessageToJson(span)
    expected_tokens = sum(
        s.attributes.get("gen_ai.usage.total_tokens", 0)
        for s in originals
        if s.name == "chat"
    )
    assert (
        sum(
            next(
                (
                    int(a.value.int_value)
                    for a in s.attributes
                    if a.key == "gen_ai.usage.total_tokens"
                ),
                0,
            )
            for s in spans
        )
        == expected_tokens
    )
    if not args.error:
        assert expected_tokens == (14 if args.async_stream else 28), expected_tokens
        assert any(s.name.startswith("execute_tool") for s in spans)
        assert all("gen_ai.span.kind" not in s.attributes for s in originals)
        assert any(s.events for s in originals) or os.environ.get(
            "OTEL_SEMCONV_STABILITY_OPT_IN"
        )
        if args.capture and not args.hide:
            payload = "".join(MessageToJson(req) for req, _ in received)
            assert "INPUT-SENTINEL" in payload and "OUTPUT-SENTINEL" in payload
    for req, compressed in received:
        assert compressed == args.gzip
        for rs in req.resource_spans:
            attrs = {a.key: a.value.string_value for a in rs.resource.attributes}
            assert attrs["project_name"] == "offline-strands"
            assert attrs["project_type"] == "observe"
    if args.tool_error:
        payload = "".join(MessageToJson(req) for req, _ in received)
        # The agent recovers and repeats the failed result in later successful
        # model/cycle spans, including the next turn. Assert the entire wire
        # payload, not only the failed tool span's status or output.
        assert "ERROR-SENTINEL" in "".join(s.to_json() for s in originals)
        if not args.capture or config.hide_error_details:
            assert "ERROR-SENTINEL" not in payload
        else:
            assert "ERROR-SENTINEL" in payload
        if args.capture and not config.hide_inputs:
            assert "INPUT-SENTINEL" in payload
        if args.capture and not config.hide_outputs:
            assert "OUTPUT-SENTINEL" in payload
    handle.uninstrument()
    try:
        configure_strands_tracing(
            tracer_provider=provider, enable_console_exporter=True
        )
    except ValueError as error:
        assert "privacy policy" in str(error)
    else:
        raise AssertionError("raw console exporter must not bypass privacy")
    assert trace.get_tracer_provider() is provider
    provider.shutdown()

server.shutdown()
server.server_close()
print(
    json.dumps(
        {"status": "PASS", "spans": len(spans), "wire": args.wire, "gzip": args.gzip}
    )
)
