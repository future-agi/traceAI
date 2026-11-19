"""Offline usage accounting with fake SDK spans and real in-memory export."""

from types import SimpleNamespace

import pytest
from agents.tracing.span_data import AgentSpanData, ResponseSpanData
from openai.types.responses import Response
from traceai_openai_agents._processor import FiTracingProcessor


def _sdk_span(span_id, data, parent_id=None):
    return SimpleNamespace(
        span_id=span_id,
        trace_id="trace_fake_usage",
        parent_id=parent_id,
        span_data=data,
        started_at="2025-01-01T00:00:00+00:00",
        ended_at="2025-01-01T00:00:01+00:00",
        error=None,
    )


@pytest.mark.parametrize("nested_in_agent", [True, False], ids=["agent", "root"])
@pytest.mark.parametrize(
    "usage_counts",
    [
        pytest.param([(10, 5, 0, 0)], id="single-response"),
        pytest.param([(10, 5, 3, 2), (20, 7, 4, 1)], id="multiple-responses"),
        pytest.param([(0, 0, 0, 0)], id="zero-usage"),
        pytest.param([None], id="missing-usage"),
    ],
)
def test_response_usage_is_only_exported_on_request_spans(
    tracer_provider, in_memory_span_exporter, usage_counts, nested_in_agent
):
    processor = FiTracingProcessor(tracer_provider.get_tracer(__name__))
    workflow = SimpleNamespace(name="usage workflow", trace_id="trace_fake_usage")
    agent = _sdk_span("span_fake_agent", AgentSpanData(name="assistant"))
    expected_usage = []

    processor.on_trace_start(workflow)
    if nested_in_agent:
        processor.on_span_start(agent)
    try:
        for index, counts in enumerate(usage_counts):
            usage = None
            attributes = {}
            if counts is not None:
                input_tokens, output_tokens, cached_tokens, reasoning_tokens = counts
                usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "input_tokens_details": {"cached_tokens": cached_tokens},
                    "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                }
                attributes = {
                    "gen_ai.usage.input_tokens": input_tokens,
                    "gen_ai.usage.output_tokens": output_tokens,
                    "gen_ai.usage.total_tokens": input_tokens + output_tokens,
                    "gen_ai.usage.input_tokens.cache_read": cached_tokens,
                    "gen_ai.usage.output_tokens.reasoning": reasoning_tokens,
                }
            expected_usage.append(attributes)
            response = Response(
                id=f"resp_fake_{index}",
                created_at=1,
                model="gpt-4o-mini",
                object="response",
                status="completed",
                parallel_tool_calls=False,
                tool_choice="none",
                tools=[],
                output=[{
                    "id": f"msg_fake_{index}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{
                        "type": "output_text",
                        "text": f"answer {index}",
                        "annotations": [],
                    }],
                }],
                usage=usage,
            )
            sdk_span = _sdk_span(
                f"span_fake_response_{index}",
                ResponseSpanData(input=f"question {index}", response=response),
                parent_id=agent.span_id if nested_in_agent else None,
            )
            processor.on_span_start(sdk_span)
            processor.on_span_end(sdk_span)
    finally:
        if nested_in_agent:
            processor.on_span_end(agent)
        processor.on_trace_end(workflow)

    spans = in_memory_span_exporter.get_finished_spans()
    requests = [span for span in spans if span.name == "response"]
    wrappers = [span for span in spans if span.name != "response"]
    assert len(requests) == len(usage_counts)
    assert len(wrappers) == (2 if nested_in_agent else 1)
    assert all(span.end_time is not None for span in spans)

    for index, (span, attributes) in enumerate(zip(requests, expected_usage)):
        assert {
            key: value for key, value in span.attributes.items()
            if key.startswith("gen_ai.usage.")
        } == attributes
        assert span.attributes["gen_ai.span.kind"] == "LLM"
        assert span.attributes["input.value"] == f"question {index}"
        assert span.attributes["output.value"] == f"answer {index}"

    # Whole-trace accounting must equal the sum of the actual provider requests.
    for key in {key for attributes in expected_usage for key in attributes}:
        assert sum(span.attributes.get(key, 0) for span in spans) == sum(
            attributes.get(key, 0) for attributes in expected_usage
        )

    for span in wrappers:
        assert not any(key.startswith("gen_ai.usage.") for key in span.attributes)
        assert span.attributes["gen_ai.request.model"] == "gpt-4o-mini"
        assert span.attributes["output.value"] == f"answer {len(usage_counts) - 1}"
        assert span.attributes[
            "gen_ai.output.messages.0.message.contents.0.message_content.text"
        ] == f"answer {len(usage_counts) - 1}"
    root = next(span for span in wrappers if span.name == workflow.name)
    assert root.attributes["input.value"] == "question 0"
    parent = next(span for span in wrappers if span.name == "assistant") if nested_in_agent else root
    assert all(span.parent.span_id == parent.context.span_id for span in requests)
