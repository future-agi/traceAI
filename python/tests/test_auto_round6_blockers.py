"""A1/A6/A7 common-path acceptance gates (loopback and synthetic data only)."""

import json
from dataclasses import fields, replace

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer, TraceConfig, register
from fi_instrumentation.auto import _manager
from fi_instrumentation.auto._config import ConfigError, resolve
from fi_instrumentation.auto.wire._privacy import export_span
from test_auto_manager import fresh_state  # noqa: F401
from auto_mock_server import CHAT_RESPONSE

PRIVACY_FIELDS = tuple(f.name.lstrip("_") for f in fields(TraceConfig))


@pytest.mark.parametrize("key", PRIVACY_FIELDS)
def test_every_file_privacy_field_is_resolved_with_source(tmp_path, monkeypatch, key):
    # A caller-provided environment must not accidentally reread the real process.
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    path = tmp_path / "futureagi.toml"
    value = 17 if key == "base64_image_max_length" else True
    path.write_text(f"{key} = {json.dumps(value)}\n")
    settings = resolve(environ={}, config_file=str(path))
    assert getattr(settings.trace_config, key) == value
    assert settings.masked()[key] == {"value": value, "source": f"file {path}"}
    if key != "hide_inputs":
        assert not settings.trace_config.hide_inputs


@pytest.mark.parametrize("key", PRIVACY_FIELDS)
def test_privacy_source_precedence_and_diagnostics(tmp_path, key):
    path = tmp_path / "futureagi.toml"
    integer = key == "base64_image_max_length"
    path.write_text(f"{key} = {17 if integer else 'true'}\n")
    env_value, arg_value = ("23", 29) if integer else ("false", True)
    environment = {"FI_" + key.upper(): env_value}
    env = resolve(environ=environment, config_file=str(path))
    assert getattr(env.trace_config, key) == (23 if integer else False)
    assert env.masked()[key]["source"] == "env FI_" + key.upper()
    explicit = resolve({key: arg_value}, environ=environment, config_file=str(path))
    assert getattr(explicit.trace_config, key) == arg_value
    assert explicit.masked()[key]["source"] == "argument"


def test_explicit_policy_shadows_file_and_preserves_derived_error_controls(tmp_path):
    path = tmp_path / "futureagi.toml"
    path.write_text("hide_inputs = malformed\nhide_outputs = malformed\n")
    policy = TraceConfig(hide_inputs=True, hide_outputs=True)
    settings = resolve(environ={}, config_file=str(path), trace_config=policy)
    assert settings.trace_config is policy
    assert settings.masked()["hide_inputs"] == {"value": True, "source": "argument"}
    assert policy.hide_error_details
    assert not replace(policy, hide_inputs=False, hide_outputs=False).hide_error_details


@pytest.mark.parametrize("line", ["hide_inputs = 5", 'hide_outputs = "maybe"',
                                  "base64_image_max_length = -1", "base64_image_max_length = true"])
def test_invalid_file_privacy_is_not_silently_ignored(tmp_path, line):
    path = tmp_path / "futureagi.toml"
    path.write_text(line + "\n")
    with pytest.raises(ConfigError):
        resolve(environ={}, config_file=str(path))


@pytest.mark.parametrize("entry", ["init", "auto", "manual"])
def test_file_hide_policy_reaches_collection_and_export(fresh_state, entry):
    (fresh_state / "futureagi.toml").write_text("hide_inputs = true\nhide_outputs = true\n")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    if entry == "init":
        _manager.init(tracer_provider=provider, instrumentations=[])
        policy = _manager._state.trace_config
    elif entry == "auto":
        _manager.auto_instrument(tracer_provider=provider, instrumentations=[])
        policy = _manager._state.trace_config
    else:
        policy = FITracer(provider.get_tracer("manual"))._self_config
    assert policy.hide_inputs and policy.hide_outputs and policy.hide_error_details
    tracer = FITracer(provider.get_tracer("configured"), config=policy)
    with tracer.start_as_current_span("hidden", attributes={
            "input.value": "PRIVATE_INPUT", "output.value": "PRIVATE_OUTPUT"}):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.attributes["input.value"] == "__REDACTED__"
    assert span.attributes["output.value"] == "__REDACTED__"
    if entry != "manual":
        assert _manager._state.settings.trace_config is policy
        _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("mode", ["init", "manual", "host"])
def test_service_identity_survives_project_routing(fresh_state, monkeypatch, mode):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "checkout-api")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.namespace=checkout,service.version=1.2.3")
    exporter = InMemorySpanExporter()
    if mode == "manual":
        provider = register(project_name="FI-project", span_exporter=exporter,
                            batch=False, verbose=False, signal_handlers=False)
    else:
        host = TracerProvider(resource=Resource.create({"host.identity": "keep"})) if mode == "host" else None
        provider = _manager.init(project_name="FI-project", span_exporter=exporter,
                                 tracer_provider=host, batch=False, instrumentations=[])
    with provider.get_tracer("app").start_as_current_span("identity"):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.resource.attributes["service.name"] == "checkout-api"
    assert span.resource.attributes["service.namespace"] == "checkout"
    assert span.resource.attributes["service.version"] == "1.2.3"
    assert span.resource.attributes["project_name"] == "FI-project"
    if mode == "host":
        assert span.resource.attributes["host.identity"] == "keep"
    if mode != "manual":
        _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("limit", [128, 32])
@pytest.mark.parametrize("mode", ["owned", "host"])
def test_long_chat_keeps_semantics_and_otlp_dropped_counts(fresh_state, limit, mode):
    exporter = InMemorySpanExporter()
    limits = SpanLimits(max_span_attributes=limit)
    options = {"tracer_provider": TracerProvider(span_limits=limits)} if mode == "host" else {"span_limits": limits}
    provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=["wire"], **options)
    payload = {"model": "gpt-4o-mini", "messages": [
        {"role": "user", "content": f"message {i}"} for i in range(100)]}
    response = httpx.Response(200, json=CHAT_RESPONSE)
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        assert client.post("http://127.0.0.1:1/v1/chat/completions", json=payload).json() == CHAT_RESPONSE
    (span,) = exporter.get_finished_spans()
    attrs = span.attributes
    expected = {"gen_ai.span.kind": "LLM", "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "openai_compatible", "traceai.capture.layer": "wire",
                "http.response.status_code": 200, "gen_ai.usage.input_tokens": 5,
                "gen_ai.usage.output_tokens": 1, "gen_ai.usage.total_tokens": 6,
                "output.value": "pong", "output.mime_type": "text/plain",
                "gen_ai.output.messages.0.message.content": "pong"}
    for key, value in expected.items():
        assert attrs[key] == value, key
    assert list(attrs["gen_ai.response.finish_reasons"]) == ["stop"]
    assert attrs["traceai.wire.endpoint"] == "openai.chat_completions"
    assert len(attrs) <= limit
    assert span.status.status_code is trace.StatusCode.OK
    assert span.dropped_attributes > 0
    # A7 (spec names): omitted content keys are counted separately from OTel's drop count;
    # the newest input messages are a contiguous suffix and the aggregate input is omitted.
    indices = sorted({int(key.split(".")[3]) for key in attrs if key.startswith("gen_ai.input.messages.")})
    assert indices == list(range(indices[0], 100))
    assert "input.value" not in attrs and "input.mime_type" not in attrs
    from fi_instrumentation._attribute_limits import _content_group
    from fi_instrumentation.auto.wire import _core

    request = _core.RequestData("POST", "http://127.0.0.1:1/v1/chat/completions",
                                {"content-type": "application/json"}, json.dumps(payload).encode())
    found = _core.match(request.method, request.url)
    candidates = _core.build(found, request, _core.ResponseData(
        status=200, headers={"content-type": "application/json"}, body=json.dumps(CHAT_RESPONSE).encode())).attributes
    content = [key for key in candidates if _content_group(key, "chat") is not None]
    emitted = [key for key in attrs if _content_group(key, "chat") is not None]
    assert attrs["traceai.capture.dropped_content_count"] == len(content) - len(emitted)
    assert len(attrs) == limit
    again = export_span(export_span(span, TraceConfig()), TraceConfig())
    assert again.dropped_attributes == span.dropped_attributes
    encoded = encode_spans([again]).resource_spans[0].scope_spans[0].spans[0]
    assert encoded.dropped_attributes_count == span.dropped_attributes
    _manager.shutdown()
    provider.shutdown()


@pytest.mark.parametrize("limit", [0, 1, 3, 16, 32, 128])
def test_library_projection_respects_tiny_host_limits_and_keeps_error_type(limit):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=limit))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("library"), config=TraceConfig())
    with tracer.start_as_current_span("chat", attributes={
            "gen_ai.span.kind": "LLM", "gen_ai.provider.name": "openai",
            "gen_ai.operation.name": "chat", "error.type": "ProviderError",
            "gen_ai.usage.input_tokens": 7}) as span:
        span.set_status(trace.Status(trace.StatusCode.ERROR, "failed"))
        for i in range(200):
            span.set_attribute(f"gen_ai.input.messages.{i}.message.content", "content")
    (ended,) = exporter.get_finished_spans()
    assert len(ended.attributes) <= limit
    assert ended.dropped_attributes > 0
    if limit >= 16:
        assert ended.attributes["gen_ai.provider.name"] == "openai"
        assert ended.attributes["error.type"] == "ProviderError"
        assert ended.attributes["gen_ai.usage.input_tokens"] == 7
    provider.shutdown()


def test_all_sdk_drop_accounting_survives_repeated_privacy_snapshots():
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=2, max_events=1,
        max_event_attributes=1, max_links=1, max_link_attributes=1))
    context = trace.SpanContext(11, 22, False, trace.TraceFlags(1))
    links = [trace.Link(context, {"first": "a", "second": "b"}) for _ in range(2)]
    raw = provider.get_tracer("sdk").start_span("loss", links=links)
    raw.set_attributes({"first": "a", "second": "b", "third": "c"})
    for _ in range(2):
        raw.add_event("event", {"first": "a", "second": "b"})
    raw.end()
    clean = export_span(export_span(raw, TraceConfig()), TraceConfig())
    assert clean.dropped_attributes == raw.dropped_attributes == 1
    assert clean.dropped_events == raw.dropped_events == 1
    assert clean.dropped_links == raw.dropped_links == 1
    assert clean.events[0].dropped_attributes == raw.events[0].dropped_attributes == 1
    assert clean.links[0].dropped_attributes == raw.links[0].dropped_attributes == 1
    provider.shutdown()
