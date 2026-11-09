"""Privacy contracts on the serialized OTLP wire using a real offline Ollama client."""
import base64
import json

import httpx
import ollama
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_ollama import OllamaInstrumentor
from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor
from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor


def detached_input(flag, request):
    """Adapter sanitization is separate from core's opaque aggregate policy."""
    return dict(_RequestAttributesExtractor(
        TraceConfig(**{flag: True})
    ).get_extra_attributes_from_request(request))["input.value"]


def detached_output(flag, response, request):
    return dict(_ResponseAttributesExtractor(
        TraceConfig(**{flag: True})
    ).get_extra_attributes(response, request))["output.value"]


PROMPT = "SYNTHETIC_PROMPT_49"
IMAGE = base64.b64encode(b"SYNTHETIC_IMAGE_49").decode()
TOKEN = "SYNTHETIC_TOKEN_49"
ALTERNATE = "SYNTHETIC_ALTERNATE_49"


@pytest.fixture
def client_span():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OllamaInstrumentor()

    def run(flag, endpoint, request, response):
        exporter.clear()
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**{flag: True}))
        client = ollama.Client(host="http://offline.invalid", transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=response)), trust_env=False)
        try:
            getattr(client, endpoint)(model="synthetic-model", **request)
            spans = exporter.get_finished_spans()
            assert len(spans) == 1
            wire = ExportTraceServiceRequest.FromString(encode_spans(spans).SerializeToString())
            span = wire.resource_spans[0].scope_spans[0].spans[0]
            return {item.key: getattr(item.value, item.value.WhichOneof("value")) for item in span.attributes}
        finally:
            client._client.close()
            instrumentor.uninstrument()

    yield run
    provider.shutdown()


@pytest.mark.parametrize("flag", ["hide_input_text", "hide_input_messages"])
def test_chat_input_text_not_in_serialized_request(client_span, flag):
    messages = [{"role": "user", "content": PROMPT}]
    attrs = client_span(flag, "chat", {"messages": messages},
                        {"model": "synthetic-model", "message": {"role": "assistant", "content": "ok"}, "done": True})
    assert PROMPT not in json.dumps(attrs)
    assert messages[0]["content"] == PROMPT  # telemetry must never mutate SDK parameters
    assert attrs["input.value"] == REDACTED_VALUE
    if flag == "hide_input_text":
        assert json.loads(detached_input(flag, {"messages": messages}))["messages"][0]["role"] == "user"
        assert attrs["gen_ai.input.messages.0.message.role"] == "user"


@pytest.mark.parametrize("flag", ["hide_input_text", "hide_input_messages"])
def test_generate_and_embed_input_text_not_serialized(client_span, flag):
    for endpoint, request, response in [
        ("generate", {"prompt": PROMPT}, {"response": "ok", "done": True}),
        ("embed", {"input": PROMPT}, {"embeddings": [[0.1]], "done": True}),
    ]:
        attrs = client_span(flag, endpoint, request, response)
        assert PROMPT not in json.dumps(attrs)


def test_chat_input_thinking_and_tool_arguments_hidden(client_span):
    messages = [{"role": "user", "content": "ok", "thinking": "SYNTHETIC_INPUT_THINK_83",
                 "tool_calls": [{"function": {"name": "lookup", "arguments": {"query": "SYNTHETIC_INPUT_TOOL_83"}}}]}]
    attrs = client_span("hide_input_text", "chat", {"messages": messages},
                        {"model": "synthetic-model", "message": {"role": "assistant", "content": "ok"}, "done": True})
    assert "SYNTHETIC_INPUT_THINK_83" not in json.dumps(attrs)
    assert "SYNTHETIC_INPUT_TOOL_83" not in json.dumps(attrs)
    assert attrs["input.value"] == REDACTED_VALUE
    message = json.loads(detached_input("hide_input_text", {"messages": messages}))["messages"][0]
    assert message["role"] == "user"
    assert message["tool_calls"][0]["function"]["name"] == "lookup"
    assert messages[0]["tool_calls"][0]["function"]["arguments"]["query"] == "SYNTHETIC_INPUT_TOOL_83"
    assert messages[0]["thinking"] == "SYNTHETIC_INPUT_THINK_83"


def test_chat_none_messages_keeps_span(client_span):
    attrs = client_span("hide_input_text", "chat", {"messages": None},
                        {"model": "synthetic-model", "message": {"role": "assistant", "content": "ok"}, "done": True})
    assert attrs["gen_ai.provider.name"] == "ollama"
    assert attrs["input.value"] == REDACTED_VALUE
    assert json.loads(detached_input("hide_input_text", {"messages": None})) == {"messages": []}


def test_chat_images_hidden_in_all_attributes(client_span):
    messages = [{"role": "user", "content": "ok", "images": [IMAGE]}]
    attrs = client_span("hide_input_images", "chat", {"messages": messages},
                        {"model": "synthetic-model", "message": {"role": "assistant", "content": "ok"}, "done": True})
    assert IMAGE not in json.dumps(attrs)
    assert messages[0]["images"] == [IMAGE]
    assert attrs["input.value"] == REDACTED_VALUE
    assert "images" not in json.loads(detached_input("hide_input_images", {"messages": messages}))["messages"][0]
    assert not any(key.endswith(".images") for key in attrs)


def test_generated_tool_arguments_hidden_under_output_text(client_span):
    response = {"model": "synthetic-model", "done": True,
                "message": {"role": "assistant", "content": "ok", "tool_calls": [
                    {"function": {"name": "lookup", "arguments": {"query": "SYNTHETIC_TOOL_ARGUMENT_83"}}}]}}
    attrs = client_span("hide_output_text", "chat", {"messages": [{"role": "user", "content": "ok"}]}, response)
    assert "SYNTHETIC_TOOL_ARGUMENT_83" not in json.dumps(attrs)
    assert attrs["output.value"] == REDACTED_VALUE
    output = json.loads(detached_output("hide_output_text", response, {"messages": [{"role": "user", "content": "ok"}]}))
    assert output["message"]["tool_calls"][0]["function"]["name"] == "lookup"
    assert output["message"]["tool_calls"][0]["function"]["arguments"] == REDACTED_VALUE
    assert attrs["gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"] == "lookup"
    assert response["message"]["tool_calls"][0]["function"]["arguments"]["query"] == "SYNTHETIC_TOOL_ARGUMENT_83"


@pytest.mark.parametrize("flag", ["hide_output_text", "hide_output_messages"])
@pytest.mark.parametrize("endpoint", ["chat", "generate"])
def test_logprob_tokens_hidden_on_output_wire(client_span, flag, endpoint):
    response = {"model": "synthetic-model", "done": True,
                "logprobs": [{"token": TOKEN, "logprob": -0.1,
                              "top_logprobs": [{"token": ALTERNATE, "logprob": -0.2}]}]}
    if endpoint == "chat":
        response["message"] = {"role": "assistant", "content": "SYNTHETIC_OUTPUT_49"}
        request = {"messages": [{"role": "user", "content": "ok"}]}
    else:
        response["response"] = "SYNTHETIC_OUTPUT_49"
        request = {"prompt": "ok"}
    attrs = client_span(flag, endpoint, request, response)
    output = json.dumps({key: value for key, value in attrs.items()
                         if key == "output.value" or key.startswith("gen_ai.output.")})
    assert TOKEN not in output
    assert ALTERNATE not in output
    assert "SYNTHETIC_OUTPUT_49" not in output
    assert attrs["output.value"] == REDACTED_VALUE
    assert "logprobs" not in json.loads(detached_output(flag, response, request))
