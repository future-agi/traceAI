"""The CrewAI projection preserves every explicit core hide setting."""

import json

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

from test_privacy_policy import SECRET, tracer_for


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "flag,key,value",
    [
        ("hide_input_images", "gen_ai.input.images.0", "https://example.invalid/image"),
        (
            "hide_llm_invocation_parameters",
            "gen_ai.request.parameters",
            json.dumps({"temperature": 0.5, "api_key": SECRET}),
        ),
        ("hide_embedding_vectors", "gen_ai.embeddings.vectors", [0.1, 0.2]),
        (
            "hide_embedding_vectors",
            "embedding.embeddings.0.embedding.vector",
            [0.1, 0.2],
        ),
    ],
)
def test_non_content_hide_flags_are_delegated_without_mutating(
    flag, key, value, enabled
):
    config = TraceConfig(**{flag: enabled})
    tracer, exporter, provider = tracer_for(config)
    with tracer.start_as_current_span(
        "policy", attributes={key: value, "client.config.api_key.value": SECRET}
    ) as span:
        span.add_event("policy.event", {key: value, "headers.Authorization.0": SECRET})
    spans = exporter.get_finished_spans()
    assert SECRET.encode() not in encode_spans(spans).SerializeToString()
    for attributes in (spans[0].attributes, spans[0].events[0].attributes):
        if enabled:
            assert key not in attributes
        else:
            assert key in attributes
    assert getattr(config, flag) is enabled
    provider.shutdown()


@pytest.mark.parametrize("enabled", [False, True])
def test_pii_setting_is_preserved_and_credentials_are_always_redacted(enabled):
    tracer, exporter, provider = tracer_for(TraceConfig(pii_redaction=enabled))
    with tracer.start_as_current_span("policy") as span:
        span.add_event(
            "log.record",
            {"log.body": json.dumps({"api_key": SECRET, "email": "test@example.com"})},
        )
    wire = encode_spans(exporter.get_finished_spans()).SerializeToString()
    assert SECRET.encode() not in wire
    assert (b"test@example.com" in wire) is not enabled
    provider.shutdown()


@pytest.mark.parametrize("enabled", [False, True])
def test_manual_error_event_attributes_stay_structurally_redacted(enabled, caplog):
    tracer, exporter, provider = tracer_for(TraceConfig(hide_error_details=enabled))
    with tracer.start_as_current_span("manual-error") as span:
        # Free-text error details are controlled separately; structured exception
        # attributes must be safe even when error-detail hiding is explicitly off.
        span.record_exception(
            RuntimeError("ordinary failure"),
            attributes={"client.config.api_key.value": SECRET, "safe": "useful error"},
        )
        span.add_event(
            "exception",
            {
                "exception.type": "RuntimeError",
                "exception.message": "ordinary failure",
                "log.body": json.dumps({"authorization": SECRET, "safe": "useful log"}),
            },
        )
        span.set_status(trace.StatusCode.ERROR, "ordinary failure")
    spans = exporter.get_finished_spans()
    wire = encode_spans(spans).SerializeToString()
    assert SECRET.encode() not in wire and SECRET not in caplog.text
    assert b"useful error" in wire and b"useful log" in wire
    assert (b"ordinary failure" in wire) is not enabled
    assert (spans[0].status.description is None) is enabled
    provider.shutdown()


@pytest.mark.parametrize(
    "flag",
    [
        "hide_input_images",
        "hide_embedding_vectors",
        "hide_llm_invocation_parameters",
        "pii_redaction",
    ],
)
def test_non_content_environment_flags_and_explicit_false_precedence(flag, monkeypatch):
    from traceai_crewai._privacy import _CrewAITraceConfig

    monkeypatch.setenv("FI_" + flag.upper(), "true")
    assert getattr(_CrewAITraceConfig(TraceConfig()), flag) is True
    assert getattr(_CrewAITraceConfig(TraceConfig(**{flag: False})), flag) is False
