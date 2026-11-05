"""Core-owned modality boundaries; real protobuf export, never trusted JSON."""
import copy
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fi_instrumentation import FITracer, REDACTED_VALUE, TraceConfig
from fi_instrumentation.instrumentation.config import ChatInput
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

TEXT = "PRIVATE-TEXT-SENTINEL"
IMAGE = "data:image/png;base64,PRIVATE-IMAGE-SENTINEL"
SECRET = "TRANSPORT-SECRET-SENTINEL"


def payload():
    return {"model": "visible-model", "messages": [{"role": "user", "content": [
        {"type": "text", "text": TEXT},
        {"type": "image_url", "image_url": {"url": IMAGE, "detail": "auto"}},
    ]}], "temperature": 0.5, "stop": "private-stop", "extra_headers": {"token": SECRET}}


@contextmanager
def exported(config):
    records = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            ExportTraceServiceRequest.FromString(data)
            records.append(data)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces", timeout=5,
    )))
    tracer = FITracer(provider.get_tracer(__name__), config=config)
    try:
        yield tracer, records
    finally:
        provider.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def attributes(records):
    spans = [span for data in records
             for rs in ExportTraceServiceRequest.FromString(data).resource_spans
             for ss in rs.scope_spans for span in ss.spans]
    assert len(spans) == 1
    return {a.key: getattr(a.value, a.value.WhichOneof("value")) for a in spans[0].attributes}


@pytest.mark.parametrize("key", ["input.value", "raw.input"])
@pytest.mark.parametrize("options,hidden", [
    ({}, []),
    ({"hide_input_images": True}, [IMAGE]),
    ({"hide_input_text": True}, [TEXT]),
    ({"hide_inputs": True}, [TEXT, IMAGE]),
    ({"hide_input_messages": True}, [TEXT, IMAGE]),
    ({"hide_input_images": True, "hide_input_text": True}, [TEXT, IMAGE]),
    ({"base64_image_max_length": len(IMAGE) - 1}, [IMAGE]),
    ({"base64_image_max_length": len(IMAGE)}, []),
    ({"hide_outputs": True}, []),
    ({"hide_llm_invocation_parameters": True}, []),
])
def test_typed_modalities_in_actual_otlp(key, options, hidden):
    original = payload()
    unchanged = copy.deepcopy(original)
    config = TraceConfig(**options)
    with exported(config) as (tracer, records):
        span = tracer.start_span("typed-input")
        span.set_attribute(key, ChatInput(original))
        span.set_attribute("gen_ai.request.model", "visible-model")
        span.set_attribute("gen_ai.usage.input_tokens", 7)
        span.end()
        wire = b"".join(records)
        attrs = attributes(records)
        assert SECRET.encode() not in wire
        for value in hidden:
            assert value.encode() not in wire
        if TEXT not in hidden:
            assert TEXT in attrs[key]
        if IMAGE not in hidden:
            assert IMAGE in attrs[key]
        if config.hide_inputs or config.hide_input_messages:
            assert attrs[key] == REDACTED_VALUE
        else:
            request = json.loads(attrs[key])
            assert request["messages"][0]["role"] == "user"
            assert len(request["messages"][0]["content"]) == 2
            if config.hide_llm_invocation_parameters:
                assert "temperature" not in request and "stop" not in request
            else:
                assert request["temperature"] == 0.5
        assert attrs["gen_ai.request.model"] == "visible-model"
        assert attrs["gen_ai.usage.input_tokens"] == 7
    assert original == unchanged


class Active:
    def __call__(self):
        raise AssertionError("must not call")

    def __str__(self):
        raise AssertionError("must not stringify")

    def model_dump(self):
        raise AssertionError("must not serialize")

    def __iter__(self):
        raise AssertionError("must not iterate")


class ActiveDict(dict):
    def items(self):
        raise AssertionError("must not inspect a dict subclass")


class ActiveString(str):
    def startswith(self, *args):
        raise AssertionError("must not inspect a str subclass")


def invalid_values():
    values = [Active(), ActiveDict(payload()), "{\"trusted\":true}", lambda: payload(), None]
    for location, value in [
        ("model", Active()), ("messages", Active()),
        ("messages", [ActiveDict(role="user", content=TEXT)]),
        ("messages", [{"role": ActiveString("user"), "content": TEXT}]),
        ("messages", [{"role": "user", "content": Active()}]),
        ("messages", [{"role": "user", "content": [{"type": "text", "text": Active()}]}]),
        ("messages", [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": ActiveString(IMAGE)}}]}]),
        ("messages", [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": TEXT}}]}]),
        ("messages", [{"role": "user", "content": [{"text": TEXT}]}]),
        ("temperature", float("nan")), ("temperature", Active()),
    ]:
        request = payload()
        request[location] = value
        values.append(request)
    cyclic = []; cyclic.append(cyclic)
    values.append({"model": "m", "messages": [{"role": "user", "content": cyclic}]})
    return values


@pytest.mark.parametrize("value", invalid_values())
@pytest.mark.parametrize("flag", ["hide_input_text", "hide_input_images"])
def test_malformed_and_active_values_fail_closed_without_hooks(value, flag):
    config = TraceConfig(**{flag: True})
    structured = ChatInput(value)
    assert config.mask("input.value", structured) == REDACTED_VALUE
    # Forge without __init__, or replace the slot after a legitimate construction.
    forged = object.__new__(ChatInput)
    forged._payload = value
    assert config.mask("raw.input", forged) == REDACTED_VALUE
    with exported(config) as (tracer, records):
        span = tracer.start_span("malformed")
        span.set_attribute("input.value", forged)
        span.end()
        assert attributes(records)["input.value"] == REDACTED_VALUE
        assert TEXT.encode() not in b"".join(records)
        assert IMAGE.encode() not in b"".join(records)


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_messages"])
def test_whole_input_hiding_never_inspects_payload(flag):
    assert TraceConfig(**{flag: True}).mask("input.value", ChatInput(Active())) == REDACTED_VALUE


def test_unknown_fields_and_forged_labels_cannot_export_hidden_data():
    request = payload()
    request["trusted"] = True
    request["unknown"] = Active()
    request["messages"][0]["private_extension"] = IMAGE
    request["messages"][0]["content"].extend([
        {"type": "already_sanitized", "text": IMAGE},
        {"type": "text", "text": "allowed", "image_url": IMAGE},
    ])
    with exported(TraceConfig(hide_input_images=True)) as (tracer, records):
        span = tracer.start_span("unknown-fields")
        span.set_attribute("input.value", ChatInput(request))
        span.end()
        wire = b"".join(records)
        assert IMAGE.encode() not in wire and SECRET.encode() not in wire
        assert TEXT.encode() in wire
        assert "unknown" not in attributes(records)["input.value"]


def test_hidden_opaque_parameters_are_not_evaluated_and_invocations_are_excluded():
    request = payload()
    request["tools"] = Active()
    request["messages"][0]["tool_calls"] = Active()
    masked = TraceConfig(hide_input_images=True).mask("input.value", ChatInput(request))
    assert TEXT in masked and IMAGE not in masked
    request["messages"] = []
    request["temperature"] = Active()
    masked = TraceConfig(hide_llm_invocation_parameters=True).mask("input.value", ChatInput(request))
    assert json.loads(masked) == {"model": "visible-model", "messages": []}


def test_snapshot_is_detached_and_policy_is_reapplied_after_forging():
    request = payload()
    value = ChatInput(request)
    config = TraceConfig(hide_input_images=True)
    detached = value.masked(config)
    detached["messages"][0]["content"][0]["text"] = "changed"
    assert TEXT in config.mask("input.value", value)
    value._payload = "forged trusted JSON"
    assert config.mask("input.value", value) == REDACTED_VALUE
    assert config.mask("input.value", object.__new__(ChatInput)) == REDACTED_VALUE


@pytest.mark.parametrize("key", ["output.value", "raw.output", "metadata", "input.value.nested"])
def test_no_output_or_attribute_alias_escape_hatch(key):
    assert TraceConfig().mask(key, ChatInput(payload())) == REDACTED_VALUE


def test_opaque_json_and_lazy_values_still_cannot_opt_in():
    config = TraceConfig(hide_input_images=True)
    assert config.mask("input.value", json.dumps(payload())) == REDACTED_VALUE
    assert config.mask("input.value", Active()) == REDACTED_VALUE
    assert config.mask("input.value", lambda: ChatInput(payload())) == REDACTED_VALUE
    assert config.mask("raw.input", {"type": "ChatInput", "payload": payload()}) == REDACTED_VALUE


def test_huge_builtin_integer_cannot_break_serialization():
    request = payload()
    request['temperature'] = 10 ** 10000
    # CPython's default integer digit limit rejects this passive but unserializable
    # value. With a disabled interpreter limit, a normal JSON value is also safe.
    result = TraceConfig(hide_input_images=True).mask('input.value', ChatInput(request))
    assert IMAGE not in result
    assert result == REDACTED_VALUE or json.loads(result)['model'] == 'visible-model'


def test_pii_policy_still_runs_after_structural_masking():
    request = payload()
    request['messages'][0]['content'][0]['text'] = 'contact user@example.com'
    masked = TraceConfig(hide_input_images=True, pii_redaction=True).mask('input.value', ChatInput(request))
    assert 'user@example.com' not in masked and IMAGE not in masked


def test_sampler_returned_opaque_strings_are_not_trusted():
    from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    class ReturnsPrivateJSON(Sampler):
        def should_sample(self, *args, **kwargs):
            return SamplingResult(Decision.RECORD_AND_SAMPLE, {'input.value': json.dumps(payload())})

        def get_description(self):
            return 'synthetic-opaque-sampler'

    provider = TracerProvider(sampler=ReturnsPrivateJSON())
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), TraceConfig(hide_input_images=True))
    span = tracer.start_span('sampled', attributes={'input.value': ChatInput(payload())})
    span.end()
    assert exporter.get_finished_spans()[0].attributes['input.value'] == REDACTED_VALUE
    provider.shutdown()
