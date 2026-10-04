"""End-to-end tests: real Agent + real Workflow through the mapping exporter.

These tests are gated on ``agent-framework`` being installed (Phase 4 dep).
They use an offline stub chat client so no API keys / HTTP are required.
"""

import os

import pytest

os.environ.setdefault("FI_API_KEY", "test-key")
os.environ.setdefault("FI_SECRET_KEY", "test-secret")

from opentelemetry import trace as trace_api  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

agent_framework = pytest.importorskip("agent_framework")

from agent_framework import (  # noqa: E402
    Agent,
    BaseChatClient,
    ChatResponse,
    Content,
    Message,
    Role,
    WorkflowBuilder,
    WorkflowContext,
)
from agent_framework._workflows._function_executor import executor  # noqa: E402
from agent_framework.observability import (  # noqa: E402
    OtelAttr,
    enable_instrumentation,
    get_function_span,
)

from traceai_agent_framework import AgentFrameworkSpanProcessor  # noqa: E402


class _StubChatClient(BaseChatClient):
    """Offline chat client returning a canned response. No HTTP."""

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        return ChatResponse(
            messages=[
                Message(
                    role=Role("assistant"),
                    contents=[Content.from_text("Sunny in Paris.")],
                )
            ],
            model="stub-v1",
            response_id="resp-1",
        )


# Workflow executors must be module-level to be picklable / re-usable.
@executor(id="upper")
async def _to_upper(text: str, ctx: WorkflowContext[str]) -> None:
    await ctx.send_message(text.upper())


@executor(id="exclaim")
async def _add_exclaim(text: str, ctx: WorkflowContext[None, str]) -> None:
    await ctx.yield_output(text + "!")


@pytest.fixture
def captured(monkeypatch):
    """Reset the global tracer provider, install our SpanProcessor + an in-memory exporter."""
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(
        trace_api,
        "_TRACER_PROVIDER_SET_ONCE",
        type(trace_api._TRACER_PROVIDER_SET_ONCE)(),
    )
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    # Our processor runs first (mutates attributes); SimpleSpanProcessor with the
    # in-memory exporter runs next and captures the mutated spans.
    provider.add_span_processor(AgentFrameworkSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace_api.set_tracer_provider(provider)
    enable_instrumentation(enable_sensitive_data=True, force=True)
    yield exporter


# ---------------------------------------------------------------------------
# AGENT path
# ---------------------------------------------------------------------------


async def test_agent_run_emits_agent_span_with_fi_attrs(captured):
    agent = Agent(_StubChatClient(), name="weather_agent", description="weather")
    await agent.run("What's the weather in Paris?")

    agent_spans = [
        s for s in captured.get_finished_spans() if s.name.startswith("invoke_agent")
    ]
    assert len(agent_spans) == 1, [s.name for s in captured.get_finished_spans()]

    attrs = agent_spans[0].attributes
    assert attrs["gen_ai.span.kind"] == "AGENT"
    # input/output values always mirror the raw gen_ai.*.messages JSON shape.
    assert attrs["input.mime_type"] == "application/json"
    assert attrs["output.mime_type"] == "application/json"
    assert attrs["input.value"] == attrs["gen_ai.input.messages"]
    assert attrs["output.value"] == attrs["gen_ai.output.messages"]
    assert attrs["gen_ai.input.messages.0.message.role"] == "user"
    assert attrs["gen_ai.input.messages.0.message.content"] == "What's the weather in Paris?"
    assert attrs["gen_ai.output.messages.0.message.role"] == "assistant"
    assert attrs["gen_ai.output.messages.0.message.content"] == "Sunny in Paris."
    # Native gen_ai.* attrs preserved.
    assert attrs["gen_ai.agent.name"] == "weather_agent"
    assert attrs["gen_ai.provider.name"] == "microsoft.agent_framework"


# ---------------------------------------------------------------------------
# WORKFLOW path
# ---------------------------------------------------------------------------


async def test_workflow_run_emits_chain_spans(captured):
    wf = (
        WorkflowBuilder(start_executor=_to_upper)
        .add_edge(_to_upper, _add_exclaim)
        .build()
    )
    await wf.run("hello")

    spans = captured.get_finished_spans()

    # workflow.run must exist with CHAIN kind
    wf_run = next((s for s in spans if s.name == "workflow.run"), None)
    assert wf_run is not None
    assert wf_run.attributes["gen_ai.span.kind"] == "CHAIN"

    # at least one executor.process.* and one edge_group.process.* span, both CHAIN
    executor_spans = [s for s in spans if s.name.startswith("executor.process")]
    edge_spans = [s for s in spans if s.name.startswith("edge_group.process")]
    assert executor_spans
    assert edge_spans
    assert all(s.attributes["gen_ai.span.kind"] == "CHAIN" for s in executor_spans)
    assert all(s.attributes["gen_ai.span.kind"] == "CHAIN" for s in edge_spans)

    # CHAIN should NOT lift input.value / output.value
    assert "input.value" not in wf_run.attributes
    assert "output.value" not in wf_run.attributes


# ---------------------------------------------------------------------------
# TOOL path: emit a tool span via the framework's helper
# ---------------------------------------------------------------------------


async def test_tool_span_emitted_via_helper_carries_fi_attrs(captured):
    tool_attrs = {
        OtelAttr.OPERATION.value: OtelAttr.TOOL_EXECUTION_OPERATION.value,
        OtelAttr.TOOL_NAME.value: "get_weather",
        OtelAttr.TOOL_CALL_ID.value: "call-x",
        OtelAttr.TOOL_TYPE.value: "function",
    }
    with get_function_span(tool_attrs) as span:
        span.set_attribute(OtelAttr.TOOL_ARGUMENTS.value, '{"city": "Paris"}')
        span.set_attribute(OtelAttr.TOOL_RESULT.value, "sunny, 22 degrees")

    tool_spans = [
        s for s in captured.get_finished_spans() if s.name.startswith("execute_tool")
    ]
    assert len(tool_spans) == 1
    attrs = tool_spans[0].attributes
    assert attrs["gen_ai.span.kind"] == "TOOL"
    assert attrs["input.value"] == '{"city": "Paris"}'
    assert attrs["input.mime_type"] == "application/json"
    assert attrs["output.value"] == "sunny, 22 degrees"
    assert attrs["output.mime_type"] == "text/plain"
    assert attrs["gen_ai.tool.name"] == "get_weather"


# ---------------------------------------------------------------------------
# All-kinds coverage in a single run
# ---------------------------------------------------------------------------


async def test_single_run_covers_agent_chain_and_tool(captured):
    # Workflow → CHAIN
    wf = (
        WorkflowBuilder(start_executor=_to_upper)
        .add_edge(_to_upper, _add_exclaim)
        .build()
    )
    await wf.run("hello")

    # Agent → AGENT
    agent = Agent(_StubChatClient(), name="weather_agent")
    await agent.run("Paris weather?")

    # Tool span via helper → TOOL
    with get_function_span(
        {
            OtelAttr.OPERATION.value: "execute_tool",
            OtelAttr.TOOL_NAME.value: "get_weather",
            OtelAttr.TOOL_CALL_ID.value: "call-1",
            OtelAttr.TOOL_TYPE.value: "function",
        }
    ) as span:
        span.set_attribute(OtelAttr.TOOL_RESULT.value, "ok")

    kinds = {
        s.attributes.get("gen_ai.span.kind")
        for s in captured.get_finished_spans()
        if s.attributes is not None
    }
    assert "AGENT" in kinds
    assert "CHAIN" in kinds
    assert "TOOL" in kinds
