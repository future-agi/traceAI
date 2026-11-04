import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    Link,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
)
from traceai_agentscope import configure
from traceai_agentscope._projection import MAX_CONTENT, project


def emit_sensitive(provider):
    context = SpanContext(
        1, 2, False, TraceFlags(1), TraceState([("vendor", "PRIVATE_TRACESTATE")])
    )
    tracer = provider.get_tracer(
        "agentscope",
        "PRIVATE_VERSION",
        "PRIVATE_SCHEMA",
        attributes={"scope.private": "PRIVATE_SCOPE"},
    )
    with tracer.start_as_current_span(
        "PRIVATE_DYNAMIC_NAME",
        links=[Link(context, {"private": "PRIVATE_LINK"})],
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.conversation.id": "session-42",
            "gen_ai.request.model": "synthetic-model",
            "gen_ai.usage.input_tokens": 7,
            "gen_ai.input.messages": json.dumps(
                [
                    {
                        "role": "user",
                        "parts": [{"type": "text", "content": "APPROVED_CONTENT"}],
                        "api_key": "PRIVATE_KEY",
                        "nested": '{"exception.stacktrace":"PRIVATE_STACK"}',
                    }
                ]
            ),
            "gen_ai.output.messages": '[{"role":"assistant","parts":[]}]',
            "arbitrary": "PRIVATE_ATTRIBUTE",
            "exception.stacktrace": "PRIVATE_STACK",
            "gen_ai.tool.definitions": "PRIVATE_SCHEMA",
        },
    ) as span:
        span.add_event("PRIVATE_EVENT", {"exception.stacktrace": "PRIVATE_EVENT_STACK"})
        span.add_event(
            "exception",
            {
                "exception.type": "ValueError",
                "exception.message": "PRIVATE_ERROR",
                "exception.stacktrace": "PRIVATE_STACK",
            },
        )
        span.set_status(Status(StatusCode.ERROR, "PRIVATE_STATUS"))


def full_record(span):
    return (
        span.to_json()
        + str(span.instrumentation_scope)
        + str(span.context.trace_state)
        + str(span.links)
    )


@pytest.mark.parametrize("capture", [False, True])
def test_all_content_channels_are_destination_local(shared, capture):
    provider, raw = shared
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest, capture_content=capture)
    emit_sensitive(provider)
    assert handle.force_flush()
    (span,) = dest.get_finished_spans()
    assert "PRIVATE_" not in full_record(span)
    assert ("APPROVED_CONTENT" in full_record(span)) is capture
    assert span.attributes["session.id"] == "session-42"
    assert span.status.status_code == StatusCode.ERROR
    assert span.links[0].context.span_id == 2
    assert span.links[0].context.trace_id == 1
    assert span.links[0].context.trace_state == TraceState()
    assert not span.links[0].attributes
    assert dict(span.resource.attributes) == {
        "project_name": "test",
        "project_type": "observe",
        "service.name": "agentscope",
    }
    (original,) = raw.get_finished_spans()
    assert "PRIVATE_ATTRIBUTE" in full_record(original)
    assert original.resource.attributes["private.resource"] == "PRIVATE_RESOURCE"
    assert original.links[0].attributes["private"] == "PRIVATE_LINK"
    assert original.status.description == "PRIVATE_STATUS"
    assert handle.shutdown()


@pytest.mark.parametrize(
    "payload",
    [
        "{broken",
        '"free text"',
        '[{"parts":[]}]',
        '{"password":"PRIVATE"}',
        '{"tool":"{\\"accessToken\\":\\"PRIVATE\\"}"}',
    ],
)
def test_structured_content_is_bounded(shared, payload):
    provider, raw = shared
    with provider.get_tracer("agentscope").start_as_current_span(
        "chat",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": payload,
        },
    ):
        pass
    span = project(raw.get_finished_spans()[0], "test", True)
    assert "PRIVATE" not in full_record(span)
    if payload == "{broken":
        assert "input.value" not in span.attributes


@pytest.mark.parametrize(
    "payload",
    [
        "x" * (MAX_CONTENT + 1),
        "[" * 1000 + "0" + "]" * 1000,
        '{"value":NaN}',
        '{"value":Infinity}',
    ],
)
def test_unsafe_content_omitted(shared, payload):
    provider, raw = shared
    with provider.get_tracer("agentscope").start_as_current_span(
        "chat",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": payload,
        },
    ):
        pass
    assert (
        "input.value"
        not in project(raw.get_finished_spans()[0], "test", True).attributes
    )


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), 2**80, True, "unexpected"]
)
def test_numeric_validation(shared, value):
    provider, raw = shared
    with provider.get_tracer("agentscope").start_as_current_span(
        "chat",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": value,
        },
    ):
        pass
    assert (
        "gen_ai.usage.input_tokens"
        not in project(raw.get_finished_spans()[0], "test", False).attributes
    )


def test_no_unrelated_or_synthetic_operations(shared):
    provider, _ = shared
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)
    for scope, op, external in (
        ("other", "chat", False),
        ("agentscope", "unknown", False),
        ("agentscope", "execute_tool", True),
    ):
        with provider.get_tracer(scope).start_as_current_span(
            "ignored",
            attributes={
                "gen_ai.operation.name": op,
                "agentscope.agent.is_external_execution": external,
            },
        ):
            pass
    assert handle.force_flush()
    assert not dest.get_finished_spans()
    assert handle.shutdown()


def test_filter_failure_drops_instead_of_exporting_native(shared, monkeypatch):
    provider, _ = shared
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)
    from traceai_agentscope import _lifecycle

    def fail(*args):
        raise ValueError("PRIVATE_ERROR")

    monkeypatch.setattr(_lifecycle, "project", fail)
    emit_sensitive(provider)
    assert handle.force_flush()
    assert not dest.get_finished_spans()
    assert handle.shutdown()
