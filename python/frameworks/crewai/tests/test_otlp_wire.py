"""Actual owned-loopback HTTP captures, not a gzip round-trip of span.to_json()."""

import base64
import copy
import gzip
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import ProxyHandler, Request, build_opener
from typing import Any

import pytest
from crewai.tools.tool_calling import ToolCalling
from fi_instrumentation import FITracer, TraceConfig
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from traceai_crewai._privacy import _CrewAITraceConfig
from traceai_crewai._wrappers import _ToolUseWrapper

SECRET = "FAKE_WIRE_CREDENTIAL_98cab1"


def otlp_json(spans):
    # OTLP/JSON follows proto JSON except trace/span IDs are hex, not base64.
    body = MessageToDict(encode_spans(spans), use_integers_for_enums=True)

    def convert(value):
        if isinstance(value, dict):
            return {
                key: (
                    base64.b64decode(item).hex()
                    if key in ("traceId", "spanId", "parentSpanId")
                    else convert(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return json.dumps(convert(body)).encode()


class JsonHTTPExporter(SpanExporter):
    """Test-only OTLP/JSON exporter; production HTTP exporter is protobuf."""

    def __init__(self, endpoint, compressed):
        self.endpoint, self.compressed = endpoint, compressed

    def export(self, spans):
        data = otlp_json(spans)
        headers = {"Content-Type": "application/json"}
        if self.compressed:
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        with build_opener(ProxyHandler({})).open(
            Request(self.endpoint, data, headers), timeout=5
        ) as response:
            assert response.status == 200
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"hide_inputs": True},
        {"hide_outputs": True},
        {"hide_input_text": True, "hide_output_messages": True},
    ],
)
def test_real_otlp_http_wire(
    encoding, compressed, settings, fails, otlp_collector, service_credentials
):
    endpoint, captured = otlp_collector
    provider = TracerProvider(shutdown_on_exit=False)
    exporter = (
        OTLPSpanExporter(
            endpoint=endpoint,
            timeout=5,
            compression=(Compression.Gzip if compressed else Compression.NoCompression),
        )
        if encoding == "protobuf"
        else JsonHTTPExporter(endpoint, compressed)
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(
        provider.get_tracer("synthetic-wire"),
        _CrewAITraceConfig(TraceConfig(**settings)),
    )
    arguments = {
        "client": {"config": {"api_key": SECRET}},
        "headers": service_credentials,
        "query": "useful query",
    }
    result = {
        "Authorization": SECRET,
        "headers": service_credentials,
        "answer": "useful answer",
        "total_tokens": 13,
    }
    error = RuntimeError(SECRET)

    def use(calling, tool_string):
        trace.get_current_span().add_event(
            "tool.log",
            {
                "log.body": json.dumps(
                    {
                        "secret": SECRET,
                        "headers": service_credentials,
                        "safe": "useful log",
                    }
                )
            },
        )
        if fails:
            raise error
        return result

    def invoke():
        return _ToolUseWrapper(tracer)(
            use,
            None,
            (ToolCalling(tool_name="lookup", arguments=arguments), "lookup"),
            {},
        )

    try:
        if fails:
            with pytest.raises(RuntimeError) as caught:
                invoke()
            assert caught.value is error
        else:
            assert invoke() is result
        assert result["Authorization"] == SECRET
        assert arguments["client"]["config"]["api_key"] == SECRET
        assert provider.force_flush()
        assert len(captured) == 1
        headers, raw = captured[0]
        assert headers["content-type"] == (
            "application/json" if encoding == "json" else "application/x-protobuf"
        )
        assert (headers.get("content-encoding") == "gzip") is compressed
        body = gzip.decompress(raw) if compressed else raw
        assert SECRET.encode() not in body and SECRET.encode() not in raw
        assert all(
            value.encode() not in body and value.encode() not in raw
            for value in service_credentials.values()
        )
        assert b"useful log" in body
        if settings.get("hide_inputs") or settings.get("hide_input_text"):
            assert b"useful query" not in body
        if settings.get("hide_outputs") or settings.get("hide_output_messages"):
            assert b"useful answer" not in body
        if not settings:
            assert b"useful query" in body
            assert (b"useful answer" in body) is not fails
        if encoding == "json":
            decoded = json.loads(body)
            span = decoded["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
            assert span["status"]["code"] == (2 if fails else 1)
        else:
            decoded = ExportTraceServiceRequest.FromString(body)
            spans = decoded.resource_spans[0].scope_spans[0].spans
            assert len(spans) == 1
            assert spans[0].status.code == (2 if fails else 1)
    finally:
        provider.shutdown()


@pytest.fixture
def otlp_collector():
    captured = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append(
                (
                    {key.lower(): value for key, value in self.headers.items()},
                    data,
                )
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"hide_inputs": True},
        {"hide_outputs": True},
        {"hide_input_text": True, "hide_output_messages": True},
    ],
)
@pytest.mark.parametrize(
    "shape",
    ["dict", "pairs", "name", "key", "header", "serialized", "flattened"],
)
def test_native_service_credentials_never_reach_otlp_http(
    shape,
    encoding,
    compressed,
    settings,
    otlp_collector,
    native_tool_usage,
    service_credentials,
):
    from traceai_crewai import CrewAIInstrumentor

    endpoint, captured = otlp_collector
    seen = []
    fields = {**service_credentials, "Accept": "application/json"}
    if shape == "pairs":
        tool_headers = [list(item) for item in fields.items()]
    elif shape in ("name", "key", "header"):
        tool_headers = [{shape: key, "value": value} for key, value in fields.items()]
    elif shape == "serialized":
        tool_headers = json.dumps(
            [{"name": key, "value": value} for key, value in fields.items()]
        )
    elif shape == "flattened":
        tool_headers = {f"headers[{key}].value": value for key, value in fields.items()}
    else:
        tool_headers = fields
    before = copy.deepcopy(tool_headers)

    def lookup(query: str, headers: Any) -> str:
        """Return synthetic service credentials without altering application data."""
        seen.append((query, copy.deepcopy(headers)))
        trace.get_current_span().add_event(
            "tool.log",
            {"log.body": json.dumps({"headers": headers, "message": "useful log"})},
        )
        return json.dumps(
            {"headers": headers, "answer": "useful answer", "total_tokens": 13}
        )

    usage = native_tool_usage(lookup)
    arguments = {"query": "useful query", "headers": tool_headers}
    calling = ToolCalling(tool_name="lookup", arguments=arguments)
    baseline = usage.use(calling, "lookup")
    provider = TracerProvider(shutdown_on_exit=False)
    exporter = (
        OTLPSpanExporter(
            endpoint=endpoint,
            timeout=5,
            compression=(Compression.Gzip if compressed else Compression.NoCompression),
        )
        if encoding == "protobuf"
        else JsonHTTPExporter(endpoint, compressed)
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CrewAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**settings))
    try:
        # Use a fresh ToolUsage to exclude native caching from the parity check.
        actual = native_tool_usage(lookup).use(calling, "lookup")
        assert actual == baseline
        assert json.loads(actual)["headers"] == before
        assert seen == [("useful query", before)] * 2
        assert calling.arguments == arguments
        assert arguments["headers"] == before
        assert provider.force_flush() and len(captured) == 1
        headers, raw = captured[0]
        assert headers["content-type"] == (
            "application/json" if encoding == "json" else "application/x-protobuf"
        )
        assert (headers.get("content-encoding") == "gzip") is compressed
        body = gzip.decompress(raw) if compressed else raw
        for credential in service_credentials.values():
            assert credential.encode() not in body and credential.encode() not in raw
        assert b"ToolUsage._use" in body and b"useful log" in body
        assert (
            b"total_tokens" in body
            or settings.get("hide_outputs")
            or settings.get("hide_output_messages")
        )
        assert (b"useful query" not in body) is bool(
            settings.get("hide_inputs") or settings.get("hide_input_text")
        )
        assert (b"useful answer" not in body) is bool(
            settings.get("hide_outputs") or settings.get("hide_output_messages")
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("scenario", ["flat-attributes", "business-data"])
def test_native_batch_projection_and_business_parity(
    encoding, compressed, scenario, otlp_collector, native_tool_usage
):
    from traceai_crewai import CrewAIInstrumentor

    # Independent, realistic OpenAI prediction details / Gemini usage / HF limit.
    payload = {
        "usage": {
            "completion_tokens_details": {
                "accepted_prediction_tokens": 3,
                "rejected_prediction_tokens": 5,
            },
            "candidates_token_count": 7,
            "thoughts_token_count": 11,
            "cached_content_token_count": 13,
            "total_tokens": 42,
        },
        "max_new_tokens": 64,
        "topics": ["authentication", "billing"],
        "categories": ["tokenization", "shipping"],
        "id": "business-id",
    }
    before = copy.deepcopy(payload)
    flat = {"request.id": "useful batch id", "usage.total_tokens": 42}
    for selector in ("name", "key", "header", "0"):
        value_field = "1" if selector == "0" else "value"
        for bracketed in (False, True):
            for reverse in (False, True):
                root = f"request.headers.{selector}.{int(bracketed)}.{int(reverse)}"

                def path(index, field):
                    return (
                        f"{root}[{index}]['{field}']"
                        if bracketed
                        else f"{root}.{index}.{field}"
                    )

                records = [
                    (path(0, selector), "Authorization"),
                    (path(0, value_field), SECRET),
                    (path(1, selector), "Accept"),
                    (path(1, value_field), "application/json"),
                ]
                flat.update(reversed(records) if reverse else records)
    flat_before = copy.deepcopy(flat)

    def lookup(data: Any) -> str:
        """Exercise the actual native ToolUsage and its current span."""
        span = trace.get_current_span()
        if scenario == "flat-attributes":
            span.set_attributes(flat)
            span.add_event("batch.log", flat)
        span.add_event("structured.log", {"log.body": json.dumps(data)})
        return json.dumps({"answer": "useful answer", **data})

    calling = ToolCalling(tool_name="lookup", arguments={"data": payload})
    baseline = native_tool_usage(lookup).use(calling, "lookup")
    endpoint, captured = otlp_collector
    provider = TracerProvider(shutdown_on_exit=False)
    exporter = (
        OTLPSpanExporter(
            endpoint=endpoint,
            timeout=5,
            compression=(Compression.Gzip if compressed else Compression.NoCompression),
        )
        if encoding == "protobuf"
        else JsonHTTPExporter(endpoint, compressed)
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CrewAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    try:
        assert native_tool_usage(lookup).use(calling, "lookup") == baseline
        assert payload == before and flat == flat_before
        assert provider.force_flush() and len(captured) == 1
        headers, raw = captured[0]
        assert (headers.get("content-encoding") == "gzip") is compressed
        body = gzip.decompress(raw) if compressed else raw
        assert SECRET.encode() not in body and b"useful answer" in body
        decoded = (
            json.loads(body)
            if encoding == "json"
            else MessageToDict(
                ExportTraceServiceRequest.FromString(body),
                use_integers_for_enums=True,
            )
        )
        spans = [
            span
            for resource in decoded["resourceSpans"]
            for scope in resource["scopeSpans"]
            for span in scope["spans"]
        ]
        assert len(spans) == 1

        def attributes(items):
            return {
                item["key"]: item["value"].get(
                    "stringValue", item["value"].get("intValue")
                )
                for item in items
            }

        attrs = attributes(spans[0]["attributes"])
        output = json.loads(attrs["output.value"])
        if isinstance(output, str):
            output = json.loads(output)
        assert output == {"answer": "useful answer", **before}
        assert json.loads(attrs["input.value"])["tool_args"]["data"] == before
        events = {
            event["name"]: attributes(event["attributes"])
            for event in spans[0]["events"]
        }
        assert json.loads(events["structured.log"]["log.body"]) == before
        if scenario == "flat-attributes":
            expected = {
                key: "__REDACTED__" if value == SECRET else value
                for key, value in flat.items()
            }
            for batch in (attrs, events["batch.log"]):
                for key, value in expected.items():
                    assert batch[key] == (str(value) if type(value) is int else value)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_raw_negative_control_detects_unprojected_synthetic_credential():
    # Do not send this deliberately unsafe baseline to even the local collector.
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("raw-baseline").start_as_current_span(
        "raw", attributes={"input.value": json.dumps({"api_key": SECRET})}
    ):
        pass
    spans = exporter.get_finished_spans()
    for data in (encode_spans(spans).SerializeToString(), otlp_json(spans)):
        assert SECRET.encode() in data
        assert SECRET.encode() in gzip.decompress(gzip.compress(data))
    provider.shutdown()


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("capture_content", [False, True])
def test_native_kickoff_structural_configuration_never_reaches_wire(
    encoding, compressed, capture_content, otlp_collector, monkeypatch, caplog
):
    """Real native kickoff, real framework credential fields, local fake LLM.

    Run this on the floor and current CrewAI. No vendor call/credential guessing:
    these canaries exist only in configuration, not application prose/content.
    """
    from importlib.metadata import version
    from unittest.mock import patch
    from crewai import Agent, Crew, Task
    from traceai_crewai import CrewAIInstrumentor

    canaries = tuple(f"SYNTHETIC_CONFIG_{letter}_9ca427" for letter in "ABCD")
    api_key, url_password, tool_key, environment_key = canaries
    monkeypatch.setenv("OPENAI_API_KEY", environment_key)
    base_url = f"https://u:{url_password}@example.invalid"
    answer = "useful structural answer"
    floor = version("crewai") == "0.41.1"
    if floor:
        # CrewAI 0.41.1 predates crewai.LLM and takes a LangChain LLM.
        from langchain_openai import ChatOpenAI
        from langchain_core.tools import BaseTool
        from langchain_core.messages import AIMessage, AIMessageChunk
        from langchain_core.outputs import (
            ChatGeneration,
            ChatGenerationChunk,
            ChatResult,
        )
        from types import SimpleNamespace

        # CrewAI's token callback otherwise downloads a tokenizer on first use.
        monkeypatch.setattr(
            "tiktoken.encoding_for_model",
            lambda _: SimpleNamespace(encode=lambda text: list(text)),
        )

        class LocalLLM(ChatOpenAI):
            def _stream(self, *args, **kwargs):
                trace.get_current_span().add_event(
                    "llm.log", {"log.body": "useful local LLM log"}
                )
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content="Final Answer: " + answer)
                )

            def _generate(self, *args, **kwargs):
                trace.get_current_span().add_event(
                    "llm.log", {"log.body": "useful local LLM log"}
                )
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(content="Final Answer: " + answer)
                        )
                    ]
                )

        llm = LocalLLM(model="gpt-4o-mini", api_key=api_key, base_url=base_url)
        assert llm.openai_api_key.get_secret_value() == api_key
        assert llm.openai_api_base == base_url
    else:
        from crewai import LLM
        from crewai.tools import BaseTool

        llm = LLM(model="openai/synthetic", api_key=api_key, base_url=base_url)
        assert llm.api_key == api_key and llm.base_url == base_url
    assert os.environ["OPENAI_API_KEY"] == environment_key

    class LocalTool(BaseTool):
        name: str = "local_lookup"
        description: str = "Look up a useful business result locally"
        api_key: str = tool_key

        def _run(self, query: str) -> str:
            return "useful business tool answer"

    def run():
        tool = LocalTool()
        agent = Agent(
            role="Useful analyst",
            goal="Answer a business question",
            backstory="Uses public business information",
            llm=llm,
            tools=[tool],
            allow_delegation=False,
        )
        task = Task(
            description="Return useful business answer",
            expected_output="A concise answer",
            agent=agent,
            tools=[tool],
        )
        crew = Crew(agents=[agent], tasks=[task], share_crew=True)
        inputs = {
            "query": "useful business query",
            "topics": ["authentication", "billing"],
        }
        before = copy.deepcopy(inputs)
        if floor:
            result = crew.kickoff(inputs=inputs)
        else:

            def respond(*args, **kwargs):
                trace.get_current_span().add_event(
                    "llm.log", {"log.body": "useful local LLM log"}
                )
                return answer

            with patch.object(type(llm), "call", side_effect=respond) as call:
                result = crew.kickoff(inputs=inputs)
            assert call.called
        assert inputs == before and tool.api_key == tool_key
        return result.raw

    baseline = run()
    assert baseline == answer
    endpoint, captured = otlp_collector
    provider = TracerProvider(shutdown_on_exit=False)
    exporter = (
        OTLPSpanExporter(
            endpoint=endpoint,
            timeout=5,
            compression=(Compression.Gzip if compressed else Compression.NoCompression),
        )
        if encoding == "protobuf"
        else JsonHTTPExporter(endpoint, compressed)
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = TraceConfig(
        hide_inputs=not capture_content, hide_outputs=not capture_content
    )
    instrumentor = CrewAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        assert run() == baseline
        assert provider.force_flush() and len(captured) >= 2
        decoded_spans = []
        for headers, raw in captured:
            assert (headers.get("content-encoding") == "gzip") is compressed
            assert headers["content-type"] == (
                "application/json" if encoding == "json" else "application/x-protobuf"
            )
            body = gzip.decompress(raw) if compressed else raw
            for canary in canaries:
                assert canary.encode() not in body and canary.encode() not in raw
                assert canary not in caplog.text
            # Ensure we're checking meaningful parsed OTLP, not empty captures.
            decoded = (
                json.loads(body)
                if encoding == "json"
                else MessageToDict(
                    ExportTraceServiceRequest.FromString(body),
                    use_integers_for_enums=True,
                )
            )
            decoded_spans.extend(
                span
                for resource in decoded["resourceSpans"]
                for scope in resource["scopeSpans"]
                for span in scope["spans"]
            )
        assert {span["name"] for span in decoded_spans} >= {
            "Task._execute_core",
            "Crew.kickoff",
        }
        wire = b"".join(
            gzip.decompress(raw) if compressed else raw for _, raw in captured
        )
        assert b"useful local LLM log" in wire
        assert (b"useful business query" in wire) is capture_content
        assert (answer.encode() in wire) is capture_content
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compressed", [False, True])
def test_contextual_payload_policy_business_and_header_shapes_on_wire(
    encoding, compressed, otlp_collector, native_tool_usage
):
    from traceai_crewai import CrewAIInstrumentor

    payload = {
        "topics": ["authentication", "billing"],
        "rows": [
            ["authentication", "public-billing"],
            ["tokenization", "public-shipping"],
        ],
        "products": [
            {"name": "Authentication handbook", "value": "public-description"},
            {"key": "tokenization", "value": "public-category"},
        ],
        "usage": {
            "accepted_prediction_tokens": 1,
            "rejected_prediction_tokens": 2,
            "candidates_token_count": 3,
            "thoughts_token_count": 4,
            "cached_content_token_count": 5,
            "max_new_tokens": 6,
            "input_tokens": 7,
            "output_tokens": 8,
            "total_tokens": 15,
            "prompt_cache_hit_tokens": 9,
            "prompt_cache_miss_tokens": 10,
            "cacheWriteInputTokens": 11,
            "toolUsePromptTokenCount": 12,
        },
        "author_email": "writer@example.invalid",
        "author_url": "https://example.invalid/writer",
        "client": {"api": {"key": SECRET}, "private": {"key": SECRET}},
        "rawHeaders": [
            "Authorization",
            SECRET,
            "X-API-Key",
            SECRET,
            "Accept",
            "application/json",
        ],
        "headers": [{"name": "Authorization", "value": [SECRET]}],
        # Undocumented names are intentionally outside the application policy.
        "custom_vendor_field": "documented application limitation",
    }
    flat = {
        "headers.0.name": "Authorization",
        "headers.0.value.0": SECRET,
        "headers[1]['key']": "X-API-Key",
        "headers[1]['value'][0]": SECRET,
    }
    before = copy.deepcopy(payload)

    def lookup(data: Any) -> str:
        """Emit synthetic application data on the actual native tool span."""
        span = trace.get_current_span()
        span.set_attributes(flat)
        span.add_event("batch.log", flat)
        span.add_event("tool.log", {"log.body": json.dumps(data)})
        return json.dumps(data)

    endpoint, captured = otlp_collector
    provider = TracerProvider(shutdown_on_exit=False)
    exporter = (
        OTLPSpanExporter(
            endpoint=endpoint,
            timeout=5,
            compression=(Compression.Gzip if compressed else Compression.NoCompression),
        )
        if encoding == "protobuf"
        else JsonHTTPExporter(endpoint, compressed)
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CrewAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        calling = ToolCalling(tool_name="lookup", arguments={"data": payload})
        assert json.loads(native_tool_usage(lookup).use(calling, "lookup")) == before
        assert payload == before
        assert provider.force_flush() and len(captured) == 1
        _, raw = captured[0]
        body = gzip.decompress(raw) if compressed else raw
        assert SECRET.encode() not in body
        decoded = (
            json.loads(body)
            if encoding == "json"
            else MessageToDict(
                ExportTraceServiceRequest.FromString(body), use_integers_for_enums=True
            )
        )
        span = decoded["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {
            attr["key"]: attr["value"]["stringValue"] for attr in span["attributes"]
        }
        output = json.loads(attrs["output.value"])
        if isinstance(output, str):
            output = json.loads(output)
        for field in (
            "topics",
            "rows",
            "products",
            "usage",
            "author_email",
            "author_url",
            "custom_vendor_field",
        ):
            assert output[field] == before[field]
        for event in span["events"]:
            if event["name"] == "tool.log":
                log = json.loads(event["attributes"][0]["value"]["stringValue"])
                for field in ("topics", "rows", "products", "usage"):
                    assert log[field] == before[field]
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
