"""Synthetic credentials, projected aliases, events, logs and error paths."""

import copy
import json
from types import SimpleNamespace

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_crewai._privacy import _CrewAITraceConfig, _credential_key, _safe_data
from traceai_crewai._wrappers import (
    _ExecuteCoreWrapper,
    _KickoffWrapper,
    _ToolUseWrapper,
)

SECRET = "FAKE_PRIVACY_CREDENTIAL_ef718a"
INPUT = "private input prose"
OUTPUT = "private output prose"
INPUT_ALIASES = (
    "input.value",
    "raw.input",
    "crew_inputs",
    "crew_agents",
    "crew_tasks",
    "formatted_description",
    "formatted_expected_output",
    "gen_ai.input.messages.0.message.content",
    "gen_ai.prompts",
    "gen_ai.prompt",
    "gen_ai.request.parameters",
    "tool.parameters",
    "gen_ai.tool.call.arguments",
    "tool_call.function.arguments",
    "message.function_call_arguments_json",
)
OUTPUT_ALIASES = (
    "output.value",
    "raw.output",
    "gen_ai.output.messages.0.message.content",
    "gen_ai.completion",
    "gen_ai.tool.call.result",
)
CONTENT_FLAGS = (
    "hide_inputs",
    "hide_input_text",
    "hide_input_messages",
    "hide_outputs",
    "hide_output_text",
    "hide_output_messages",
)


def tracer_for(config):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return (
        FITracer(provider.get_tracer("synthetic-crewai"), _CrewAITraceConfig(config)),
        exporter,
        provider,
    )


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "apiKey",
        "API-KEY",
        "X-API-Key",
        "Authorization",
        "Proxy-Authorization",
        "access_token",
        "refreshToken",
        "auth_token",
        "authentication_token",
        "client_secret",
        "clientSecretKey",
        "secret_key",
        "password",
        "passwd",
        "token",
        "session_token",
        "bearer_token",
        "private_key",
        "client.config.api_key",
        "client.config.client_key",
        "client.config.token",
        "client.config.secret",
        "headers.Authorization",
        "aws_secret_access_key",
        "aws_access_key_id",
        "credentials",
        "Cookie",
        "Set-Cookie",
        "headers.Authorization.0",
        "headers[X-API-Key]",
        "client.config.api_key.value",
        "client.config.token.value",
        "client.config.secret.value",
        "client.config.secrets.value",
        "apiSecret",
        "secret_token",
        "X-Amz-Security-Token",
        "X-Vault-Token",
        "X-Consul-Token",
        "X-Subject-Token",
        "X-Api-Secret",
        "X-Service-Token",
        "security_token",
        "aws_security_token",
        "vault_token",
        "consul_token",
        "subject_token",
        "service_token",
        "headers.x-aMz-SeCuRiTy-ToKeN",
        "headers[X-Vault-Token][0]",
        "headers.X-Consul-Token.value",
        "headers[X-Subject-Token]",
        "headers.X-Api-Secret.0",
        "client.config.security_token.value",
    ],
)
def test_credential_spellings_and_business_fields(key):
    payload = {
        key: SECRET,
        "model": "safe-model",
        "provider": "safe-provider",
        "total_tokens": 42,
        "prompt_tokens": 11,
        "completion_tokens": 31,
        "token_count": 42,
        "tokenizer": "safe",
        "max_tokens": 90,
        "token_id": "safe-id",
        "security_token_id": "safe-security-id",
        "vault_token_count": 3,
        "consul_token_ttl": 3600,
        "subject_token_type": "safe-type",
        "service_token_count": 2,
        "api_secret_id": "safe-secret-id",
        "id": "business-id",
    }
    before = copy.deepcopy(payload)
    projected = _safe_data(payload)
    assert SECRET not in json.dumps(projected)
    assert projected[key] == "__REDACTED__"
    assert {k: v for k, v in projected.items() if k != key} == {
        k: v for k, v in payload.items() if k != key
    }
    assert payload == before


@pytest.mark.parametrize("flag", CONTENT_FLAGS)
@pytest.mark.parametrize("enabled", [True, False])
def test_every_directional_hide_flag_covers_aliases_and_events(
    flag, enabled, caplog, service_credentials
):
    tracer, exporter, provider = tracer_for(TraceConfig(**{flag: enabled}))
    attrs = {
        **{k: INPUT for k in INPUT_ALIASES},
        **{k: OUTPUT for k in OUTPUT_ALIASES},
        "headers.Authorization": SECRET,
        "gen_ai.usage.total_tokens": 42,
        "log.body": json.dumps(
            {"api_key": SECRET, "headers": service_credentials, "message": "useful log"}
        ),
        **{f"headers.{key}": value for key, value in service_credentials.items()},
    }
    before = copy.deepcopy(attrs)
    with tracer.start_as_current_span("privacy", attributes=attrs) as span:
        span.set_attributes(attrs)
        trace.get_current_span().add_event("tool.result", attributes=attrs)
    spans = exporter.get_finished_spans()
    wire = encode_spans(spans).SerializeToString()
    assert SECRET.encode() not in wire
    assert b"useful log" in wire and b"tool.result" in wire
    for credential in service_credentials.values():
        assert credential.encode() not in wire and credential not in caplog.text
    assert SECRET not in caplog.text
    if enabled:
        hidden, visible = (INPUT, OUTPUT) if "input" in flag else (OUTPUT, INPUT)
        assert hidden.encode() not in wire and visible.encode() in wire
    else:
        assert INPUT.encode() in wire and OUTPUT.encode() in wire
    assert attrs == before
    assert spans[0].attributes["gen_ai.usage.total_tokens"] == 42
    provider.shutdown()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"hide_inputs": False, "hide_outputs": False, "hide_error_details": False},
        {"hide_inputs": True, "hide_outputs": True},
        {"hide_input_messages": True, "hide_output_text": True},
        {
            "hide_inputs": True,
            "hide_output_messages": True,
            "hide_error_details": False,
        },
        {"hide_inputs": False, "hide_outputs": True, "hide_error_details": True},
        {"hide_input_images": True},
        {"hide_embedding_vectors": True},
        {"hide_llm_invocation_parameters": True},
        {"pii_redaction": True},
    ],
)
def test_default_and_explicit_combinations_always_redact_structured_aliases(
    kwargs, service_credentials
):
    tracer, exporter, provider = tracer_for(TraceConfig(**kwargs))
    structured = json.dumps(
        {
            "client": {"config": {"api_key": SECRET}},
            "headers": service_credentials,
            "safe": "business",
        }
    )
    attrs = {k: structured for k in INPUT_ALIASES + OUTPUT_ALIASES}
    with tracer.start_as_current_span("privacy", attributes=attrs) as span:
        span.add_event("structured", attrs)
    wire = encode_spans(exporter.get_finished_spans()).SerializeToString()
    assert SECRET.encode() not in wire
    assert all(value.encode() not in wire for value in service_credentials.values())
    assert (
        b"business" in wire
        or (kwargs.get("hide_inputs") and kwargs.get("hide_outputs"))
        or (kwargs.get("hide_input_messages") and kwargs.get("hide_output_text"))
        or (kwargs.get("hide_inputs") and kwargs.get("hide_output_messages"))
    )
    provider.shutdown()


@pytest.mark.parametrize("flag", CONTENT_FLAGS + ("hide_error_details",))
def test_environment_flags_and_explicit_false_precedence(flag, monkeypatch):
    monkeypatch.setenv("FI_" + flag.upper(), "true")
    config = _CrewAITraceConfig(TraceConfig())
    assert getattr(config, flag) is True
    explicit = _CrewAITraceConfig(TraceConfig(**{flag: False}))
    assert getattr(explicit, flag) is False
    assert explicit.mask("headers.Authorization", SECRET) == "__REDACTED__"


@pytest.mark.parametrize(
    "wrapper_type", [_ExecuteCoreWrapper, _KickoffWrapper, _ToolUseWrapper]
)
@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"hide_error_details": False},
        {"hide_error_details": True},
        {"hide_inputs": True, "hide_error_details": False},
    ],
)
def test_wrapper_errors_propagate_without_messages_events_or_logs(
    wrapper_type, settings, caplog
):
    tracer, exporter, provider = tracer_for(TraceConfig(**settings))
    instance = SimpleNamespace(
        key="safe-key",
        id="safe-id",
        description="task",
        expected_output="answer",
        agents=[],
        tasks=[],
        usage_metrics=None,
    )
    error = RuntimeError(SECRET)

    def fail(**kwargs):
        raise error

    with pytest.raises(RuntimeError) as caught:
        wrapper_type(tracer)(fail, instance, (), {})
    assert caught.value is error
    spans = exporter.get_finished_spans()
    assert len(spans) == 1 and spans[0].status.status_code is trace.StatusCode.ERROR
    assert not spans[0].events and spans[0].status.description is None
    assert SECRET.encode() not in encode_spans(spans).SerializeToString()
    assert SECRET not in caplog.text
    provider.shutdown()


def test_depth_cycles_and_opaque_objects_are_not_serialized():
    class Opaque:
        def __getattribute__(self, name):
            raise AssertionError("opaque object inspected")

        def __repr__(self):
            raise AssertionError("opaque object stringified")

    cycle = {"safe": "business", "api_key": SECRET, "runtime": Opaque()}
    cycle["cycle"] = cycle
    assert SECRET not in json.dumps(_safe_data(cycle))
    assert _safe_data("free text " + SECRET) == "free text " + SECRET
    assert not _credential_key("gen_ai.usage.total_tokens")


def test_context_is_redacted_before_sampler_and_at_final_setters():
    from fi_instrumentation import using_attributes
    from opentelemetry.sdk.trace.sampling import Sampler, ALWAYS_ON

    observed = []

    class InspectSampler(Sampler):
        def should_sample(self, *args, **kwargs):
            observed.append(dict(kwargs.get("attributes") or args[4]))
            return ALWAYS_ON.should_sample(*args, **kwargs)

        def get_description(self):
            return "synthetic-inspection"

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=InspectSampler(), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("context"), _CrewAITraceConfig(TraceConfig()))
    instance = SimpleNamespace(
        description="task", expected_output="answer", key="safe-key", id="safe-id"
    )

    def execute(agent=None, context=None):
        return {"api_key": SECRET, "answer": "business output"}

    with using_attributes(
        metadata={
            "client": {"config": {"api_key": SECRET}},
            "safe": "business metadata",
        }
    ):
        _ExecuteCoreWrapper(tracer)(execute, instance, (), {})
    assert len(observed) == 1
    assert SECRET not in json.dumps(observed)
    assert "business metadata" in json.dumps(observed)
    wire = encode_spans(exporter.get_finished_spans()).SerializeToString()
    assert (
        SECRET.encode() not in wire
        and b"business metadata" in wire
        and b"business output" in wire
    )
    provider.shutdown()


def test_unparseable_or_oversized_structured_data_fails_closed():
    deeply_nested = "[" * 1100 + json.dumps({"api_key": SECRET}) + "]" * 1100
    malformed = '{"api_key": "' + SECRET
    oversized = json.dumps({"api_key": SECRET, "text": "x" * 1_000_001})
    for value in (deeply_nested, malformed, oversized):
        assert SECRET not in _safe_data(value)
