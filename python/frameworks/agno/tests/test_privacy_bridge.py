"""Shared FI policy must cover all write paths, not just span attributes."""

import json
from dataclasses import fields

import pytest
from openinference.instrumentation import TraceConfig
from opentelemetry import trace
from traceai_agno._wrappers._privacy import AgnoTracer, privacy_configs


@pytest.mark.parametrize(
    "name",
    [
        "hide_inputs",
        "hide_outputs",
        "hide_input_messages",
        "hide_output_messages",
        "hide_input_text",
        "hide_output_text",
        "hide_input_images",
        "hide_embedding_vectors",
        "hide_llm_invocation_parameters",
    ],
)
def test_fi_hides_cannot_be_undone_by_openinference(monkeypatch, name):
    monkeypatch.setenv("FI_" + name.upper(), "true")
    supplied = TraceConfig(**{name: False})
    effective, fi = privacy_configs(supplied)
    assert getattr(effective, name) is True
    assert getattr(fi, name) is True
    assert getattr(supplied, name) is False
    for field in fields(supplied):
        if field.name == "base64_image_max_length":
            # A supplied OI config without an explicit cap resolves to the FI default 0.
            assert getattr(effective, field.name) == 0
        elif field.name != name:
            assert getattr(effective, field.name) == getattr(supplied, field.name)


@pytest.mark.parametrize("error_setting", ["true", "false", None])
def test_error_policy_keeps_shared_derivation_and_opt_out(
    tracing, monkeypatch, error_setting
):
    from fi_instrumentation.instrumentation import TraceConfig as FITraceConfig

    provider, exporter = tracing
    if error_setting is None:
        monkeypatch.delenv("FI_HIDE_ERROR_DETAILS", raising=False)
    else:
        monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", error_setting)
    oi, fi = privacy_configs(TraceConfig(hide_inputs=True))
    assert fi.hide_error_details == FITraceConfig(hide_inputs=True).hide_error_details
    tracer = AgnoTracer(provider.get_tracer("privacy"), oi, fi)
    secret = "synthetic-error-text"
    with tracer.start_as_current_span(
        "failure", attributes={"error.message": secret}
    ) as span:
        span.set_attribute("exception.message", secret)
        span.set_attributes({"exception.stacktrace": secret})
        span.add_event(
            "exception",
            {
                "exception.message": secret,
                "exception.stacktrace": secret,
                "exception.type": "ValueError",
            },
        )
        span.record_exception(ValueError(secret))
        span.set_status(trace.Status(trace.StatusCode.ERROR, secret))
    result = exporter.get_finished_spans()[0]
    assert result.status.status_code == trace.StatusCode.ERROR
    encoded = json.dumps(
        {
            "attributes": dict(result.attributes),
            "description": result.status.description,
            "events": [dict(e.attributes) for e in result.events],
        }
    )
    assert (secret in encoded) == (error_setting == "false")
    assert "ValueError" in encoded
    # The supplied provider and its unrelated spans have not been modified.
    with provider.get_tracer("unrelated").start_as_current_span(
        "unrelated"
    ) as unrelated:
        unrelated.set_attribute("error.message", secret)
    assert exporter.get_finished_spans()[-1].attributes["error.message"] == secret


@pytest.mark.parametrize("fi_limit", [None, "16000", "96000"])
def test_openinference_image_limit_is_preserved_unless_fi_is_stricter(
    monkeypatch, fi_limit
):
    monkeypatch.delenv("FI_BASE64_IMAGE_MAX_LENGTH", raising=False)
    if fi_limit is not None:
        monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", fi_limit)
    supplied = TraceConfig(base64_image_max_length=64000)
    oi, fi = privacy_configs(supplied)
    expected = min(64000, int(fi_limit)) if fi_limit else 64000
    assert oi.base64_image_max_length == fi.base64_image_max_length == expected
    assert supplied.base64_image_max_length == 64000
