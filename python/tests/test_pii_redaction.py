"""PR244/#195 controls reconciled with dev's whole-span redaction pipeline."""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.instrumentation.pii_redaction import (
    key_carries_free_text,
    redact_pii_in_string,
    redact_pii_in_value,
)


EMAIL = "jane@example.com"
UUID = "73630065-0794-4450-a1f9-8cc987a02b09"
STRUCTURED_KEYS = (
    "session.id",
    "user.id",
    "gen_ai.conversation.id",
    "conversation.id",
    "metadata",
    "metadata.request_id",
    "metadata.message.content",
)


@pytest.mark.parametrize("key", STRUCTURED_KEYS)
@pytest.mark.parametrize("value", (UUID, "1234567890123", EMAIL))
def test_structured_values_are_exempt_from_pii(clean_env, key, value):
    config = TraceConfig(pii_redaction=True)
    assert not key_carries_free_text(key)
    assert config.mask(key, value) == value


@pytest.mark.parametrize("key", STRUCTURED_KEYS)
def test_image_cap_is_not_bypassed_by_pii_exemption(clean_env, key):
    config = TraceConfig(pii_redaction=True)
    assert config.mask(key, "data:image/png;base64,AAAA") == "[image-redacted]"


@pytest.mark.parametrize(
    "key",
    (
        "input.value",
        "output.value",
        "raw.input",
        "raw.output",
        "gen_ai.input.messages",
        "gen_ai.input.messages.0.message.content",
        "gen_ai.output.messages.0.message.contents.0.message_content.text",
        "gen_ai.system_instructions",
        "gen_ai.prompt.template.variables",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "retrieval.documents.0.document.content",
        "embedding.embeddings.0.embedding.text",
        "exception.message",
        "exception.stacktrace",
        "error.message",
        "retry.reason",
        "extra",
        "later",
        "value",
        "metadata_extra",
        "user.identity_note",
    ),
)
def test_existing_free_text_and_custom_key_coverage_is_retained(clean_env, key):
    config = TraceConfig(pii_redaction=True)
    assert key_carries_free_text(key)
    assert config.mask(key, EMAIL) == "<EMAIL_ADDRESS>"


def test_unkeyed_value_redaction_still_protects_exception_and_status_text(clean_env):
    config = TraceConfig(pii_redaction=True)
    assert config.redact_value(EMAIL) == "<EMAIL_ADDRESS>"
    assert config.redact_value(EMAIL, False) == "<EMAIL_ADDRESS>"
    assert config.redact_value(EMAIL, attribute_key="session.id") == EMAIL
    assert TraceConfig(pii_redaction=False).redact_value(EMAIL) == EMAIL


@pytest.mark.parametrize("value", (0, 12345678901234567, 3.14159, False, None))
def test_numeric_and_other_non_string_values_are_unchanged(clean_env, value):
    assert TraceConfig(pii_redaction=True).mask("input.value", value) == value


@pytest.mark.parametrize(
    "text",
    (
        "4111111111111111",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "5555 5555 5555 4444",
        "3782 822463 10005",
        "3056 930902 5904",
        "4222 2222 2222 2",
        "4000 0000 0000 0000 006",
        "４１１１ １１１１ １１１１ １１１１",
    ),
)
def test_valid_card_candidates_are_redacted(text):
    assert redact_pii_in_string("card " + text + " today") == "card <CREDIT_CARD> today"


@pytest.mark.parametrize(
    "text",
    (
        "request 20240115103045123 failed",
        "request 1234567890123 failed",
        "request 4111111111111112 failed",
        "request " + UUID + " failed",
        "request 1234567890123456789012345 failed",
    ),
)
def test_non_card_numeric_content_survives_both_card_and_phone_passes(text):
    assert redact_pii_in_string(text) == text


@pytest.mark.parametrize("phone", ("212-555-0199", "(212) 555-0199", "+1 (212) 555-0199", "2125550199"))
def test_phone_boundary_keeps_complete_phone_replacement(phone):
    assert redact_pii_in_string("call " + phone + " today") == "call <PHONE_NUMBER> today"


def test_card_followed_by_phone_keeps_both_redactions():
    assert redact_pii_in_string("4111 1111 1111 1111 212-555-0199") == (
        "<CREDIT_CARD> <PHONE_NUMBER>"
    )


@pytest.mark.parametrize("container", (list, tuple))
def test_sequence_shape_and_unchanged_identity_survive(container):
    original = container(["jane@example.com", "request 20240115103045123", 7])
    result = redact_pii_in_value(original)
    assert type(result) is container
    assert result == container(["<EMAIL_ADDRESS>", "request 20240115103045123", 7])
    unchanged = container(["ordinary text", 7])
    assert redact_pii_in_value(unchanged) is unchanged


def test_exported_surfaces_keep_correlation_and_redact_custom_content(clean_env):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(pii_redaction=True))
    context = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(1))
    original = {"session.id": UUID, "user.id": "1234567890123", "metadata": EMAIL, "extra": EMAIL}
    try:
        span = tracer.start_span("case", attributes=original, links=[Link(context, attributes=original)])
        span.set_attribute("gen_ai.conversation.id", UUID)
        span.set_attribute("later", EMAIL)
        span.add_event("retry", attributes=original)
        try:
            raise ValueError(EMAIL)
        except ValueError as error:
            span.record_exception(error, attributes=original)
            span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()
        (exported,) = exporter.get_finished_spans()
        collections = [exported.attributes, exported.links[0].attributes]
        collections.extend(event.attributes for event in exported.events)
        for attributes in collections:
            assert attributes["session.id"] == UUID
            assert attributes["user.id"] == "1234567890123"
            assert attributes["metadata"] == EMAIL
            assert attributes["extra"] == "<EMAIL_ADDRESS>"
        assert exported.attributes["gen_ai.conversation.id"] == UUID
        assert exported.attributes["later"] == "<EMAIL_ADDRESS>"
        assert exported.status.description == "<EMAIL_ADDRESS>"
        (exception,) = [event for event in exported.events if event.name == "exception"]
        assert exception.attributes["exception.message"] == "<EMAIL_ADDRESS>"
        assert EMAIL not in exception.attributes["exception.stacktrace"]
        assert original["extra"] == EMAIL
    finally:
        provider.shutdown()
