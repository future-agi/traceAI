"""Offline contracts using the SDK's real trace/processor lifecycle, not mocks."""
import json

from agents.tracing import agent_span, function_span, generation_span, response_span, trace
from openai.types.responses import Response
from opentelemetry.trace import StatusCode


def test_generation_and_function_lifecycle(instrument, in_memory_span_exporter):
    with trace("offline workflow"):
        with agent_span("assistant"):
            with generation_span(
                input=[{"role": "user", "content": "hello"}],
                output=[{"role": "assistant", "content": "world"}],
                model="gpt-4",
                usage={"input_tokens": 3, "output_tokens": 5},
            ):
                pass
            with function_span("lookup", input='{"id": 1}', output='{"value": 2}'):
                pass

    spans = in_memory_span_exporter.get_finished_spans()
    assert len(spans) == 4
    by_name = {span.name: span for span in spans}
    root = by_name["offline workflow"]
    agent = by_name["assistant"]
    assert root.parent is None
    assert root.attributes["gen_ai.span.kind"] == "AGENT"
    assert agent.parent.span_id == root.context.span_id
    for name in ("generation", "lookup"):
        assert by_name[name].parent.span_id == agent.context.span_id
    assert all(s.end_time >= s.start_time for s in spans)
    assert all(s.status.status_code == StatusCode.OK for s in spans)
    generation = by_name["generation"].attributes
    assert generation["gen_ai.provider.name"] == "openai"
    assert generation["gen_ai.request.model"] == "gpt-4"
    assert generation["gen_ai.usage.input_tokens"] == 3
    assert generation["gen_ai.usage.output_tokens"] == 5
    assert json.loads(generation["input.value"]) == [{"role": "user", "content": "hello"}]
    assert json.loads(generation["output.value"]) == [{"role": "assistant", "content": "world"}]
    assert by_name["lookup"].attributes["gen_ai.tool.name"] == "lookup"
    assert by_name["lookup"].attributes["output.value"] == '{"value": 2}'


def test_response_output_propagates_to_agent_and_root(instrument, in_memory_span_exporter):
    response = Response(
        id="resp_offline", created_at=1234567890, model="gpt-4", object="response",
        parallel_tool_calls=False, tool_choice="none", tools=[], status="completed",
        output=[{
            "id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "hello back", "annotations": []}],
        }],
    )
    with trace("response workflow"):
        with agent_span("assistant"):
            with response_span(response=response) as sdk_span:
                sdk_span.span_data.input = "hello"

    spans = in_memory_span_exporter.get_finished_spans()
    assert len(spans) == 3
    for span in spans:
        assert span.attributes["input.value"] == "hello"
        assert span.attributes["output.value"] == "hello back"
        assert span.attributes["gen_ai.request.model"] == "gpt-4"
        assert "raw.output" not in span.attributes


def test_sdk_error_is_exported(instrument, in_memory_span_exporter):
    with trace("error workflow"):
        with function_span("broken tool") as sdk_span:
            sdk_span.set_error({"message": "tool failed", "data": {"reason": "offline"}})
    spans = in_memory_span_exporter.get_finished_spans()
    assert len(spans) == 2
    tool = next(s for s in spans if s.name == "broken tool")
    assert tool.status.status_code == StatusCode.ERROR
    assert "tool failed" in tool.status.description
    assert tool.end_time is not None
