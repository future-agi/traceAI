"""Offline regressions for T2-AGENTS-001 using fake SDK lifecycle events."""

import json
from types import SimpleNamespace

import pytest
from agents.tracing.span_data import AgentSpanData, ResponseSpanData
from fi_instrumentation import FITracer, TraceConfig
from openai.types.responses import Response
from traceai_openai_agents._processor import FiTracingProcessor


PRIVATE_MARKERS = (
    "PRIVATE_POLICY_MARKER",
    "PRIVATE_TOOL_MARKER",
    "PRIVATE_METADATA_MARKER",
    "PRIVATE_FUTURE_FIELD_MARKER",
)


def _response(output_kind):
    if output_kind == "tool-only":
        output = [{
            "id": "fc_fake", "type": "function_call", "call_id": "call_fake",
            "name": "lookup", "arguments": '{"query":"public output"}',
            "status": "completed",
        }]
    elif output_kind == "empty":
        output = []
    else:
        content = (
            {"type": "refusal", "refusal": "Cannot answer this request"}
            if output_kind == "refusal"
            else {"type": "output_text", "text": "Public answer", "annotations": []}
        )
        output = [{
            "id": "msg_fake", "type": "message", "role": "assistant",
            "status": "completed", "content": [content],
        }]
    return Response(
        id="resp_fake",
        created_at=1,
        model="gpt-4o-mini",
        object="response",
        status="completed",
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[{
            "type": "function", "name": "lookup", "description": PRIVATE_MARKERS[1],
            "parameters": {"type": "object", "properties": {}}, "strict": False,
        }],
        instructions=PRIVATE_MARKERS[0],
        metadata={"private_note": PRIVATE_MARKERS[2]},
        future_request_echo=PRIVATE_MARKERS[3],
        temperature=0.2,
        max_output_tokens=64,
        output=output,
        usage={
            "input_tokens": 3, "output_tokens": 5, "total_tokens": 8,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    )


def _export_response(response, config, tracer_provider):
    # Drive the processor directly: no Runner, API client, network, or global
    # SDK processor registration. FITracer applies the actual privacy policy.
    processor = FiTracingProcessor(
        FITracer(tracer_provider.get_tracer(__name__), config=config)
    )
    trace = SimpleNamespace(name="privacy workflow", trace_id="trace_fake")
    agent = SimpleNamespace(
        trace_id=trace.trace_id,
        span_id="agent_fake",
        parent_id=None,
        span_data=AgentSpanData(name="Assistant"),
        started_at="2025-01-01T00:00:00+00:00",
        ended_at="2025-01-01T00:00:02+00:00",
        error=None,
    )
    response_data = ResponseSpanData(response=response)
    response_data.input = "hello"
    response_span = SimpleNamespace(
        trace_id=trace.trace_id,
        span_id="response_fake",
        parent_id=agent.span_id,
        span_data=response_data,
        started_at="2025-01-01T00:00:00+00:00",
        ended_at="2025-01-01T00:00:01+00:00",
        error=None,
    )
    processor.on_trace_start(trace)
    processor.on_span_start(agent)
    try:
        processor.on_span_start(response_span)
        try:
            pass
        finally:
            processor.on_span_end(response_span)
    finally:
        processor.on_span_end(agent)
        processor.on_trace_end(trace)


@pytest.mark.parametrize("output_kind", ["tool-only", "empty", "refusal", "text"])
@pytest.mark.parametrize(
    "hide_inputs,hide_outputs",
    [
        pytest.param(True, False, id="inputs-hidden"),
        pytest.param(True, True, id="both-hidden"),
        pytest.param(False, False, id="visible"),
    ],
)
def test_response_output_excludes_request_echoes_on_all_spans(
    output_kind, hide_inputs, hide_outputs, tracer_provider, in_memory_span_exporter
):
    response = _response(output_kind)
    original = response.model_dump(mode="json")
    assert original["future_request_echo"] == PRIVATE_MARKERS[3]
    config = TraceConfig(
        hide_inputs=hide_inputs,
        hide_outputs=hide_outputs,
        hide_input_messages=False,
        hide_input_text=False,
        hide_output_messages=False,
        hide_output_text=False,
        hide_llm_invocation_parameters=False,
        pii_redaction=False,
    )

    _export_response(response, config, tracer_provider)

    spans = in_memory_span_exporter.get_finished_spans()
    assert len(spans) == 3
    assert {span.name for span in spans} == {"response", "Assistant", "privacy workflow"}
    expected_output = {
        key: original[key]
        for key in (
            "id", "object", "created_at", "model", "status", "output", "usage",
            "incomplete_details",
        )
        if original.get(key) is not None
    }
    for span in spans:
        attributes = span.attributes
        assert attributes["input.value"] == ("__REDACTED__" if hide_inputs else "hello")
        output = attributes["output.value"]
        assert all(marker not in output for marker in PRIVATE_MARKERS)
        if hide_outputs:
            assert output == "__REDACTED__"
        elif output_kind == "text":
            assert output == "Public answer"
        else:
            # Preserve tool arguments, refusals, empty outputs and usage, rather
            # than suppressing legitimate output merely because inputs are hidden.
            assert json.loads(output) == expected_output
        if hide_inputs:
            assert all(marker not in json.dumps(dict(attributes)) for marker in PRIVATE_MARKERS)
        else:
            # Request information still belongs to its original input direction.
            assert attributes["gen_ai.input.messages.0.message.content"] == PRIVATE_MARKERS[0]
        assert attributes["gen_ai.request.model"] == "gpt-4o-mini"
        # Canonical usage belongs to the response request, not its wrappers.
        usage = {
            key: value for key, value in attributes.items()
            if key.startswith("gen_ai.usage.")
        }
        if span.name == "response":
            assert usage == {
                "gen_ai.usage.input_tokens": 3,
                "gen_ai.usage.output_tokens": 5,
                "gen_ai.usage.total_tokens": 8,
                "gen_ai.usage.input_tokens.cache_read": 0,
                "gen_ai.usage.output_tokens.reasoning": 0,
            }
        else:
            assert usage == {}

    for key, expected in (
        ("gen_ai.usage.input_tokens", 3),
        ("gen_ai.usage.output_tokens", 5),
        ("gen_ai.usage.total_tokens", 8),
    ):
        assert sum(span.attributes.get(key, 0) for span in spans) == expected

    # Telemetry filtering must not alter the response the application receives.
    assert response.model_dump(mode="json") == original
