"""Real CrewAI Agent/Task/Crew and ToolUsage, using a local fake LLM/tool."""

import json
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from crewai import Agent, Crew, Task
from crewai.tools.tool_calling import ToolCalling
from crewai.tools.tool_usage import ToolUsage
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import TraceConfig
from traceai_crewai import CrewAIInstrumentor

SECRET = "FAKE_NATIVE_CREDENTIAL_a7130d"
FLOOR = version("crewai") == "0.41.1"


def run_crew():
    response = json.dumps({"api_key": SECRET, "answer": "useful answer"})
    if FLOOR:
        from langchain_core.language_models.fake import FakeListLLM

        llm = FakeListLLM(responses=["Final Answer: " + response])
        agent = Agent(
            role="Synthetic worker",
            goal="Answer",
            backstory="Local test",
            llm=llm,
            allow_delegation=False,
        )
        task = Task(
            description="Return useful answer",
            expected_output="JSON answer",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], share_crew=True)
        result = crew.kickoff(
            inputs={"client": {"config": {"api_key": SECRET}}, "tag": "useful input"}
        )
    else:
        from crewai import LLM

        llm = LLM(
            model="openai/synthetic", api_key=SECRET, base_url="http://127.0.0.1:1/v1"
        )
        agent = Agent(
            role="Synthetic worker",
            goal="Answer",
            backstory="Local test",
            llm=llm,
            allow_delegation=False,
        )
        task = Task(
            description="Return useful answer",
            expected_output="JSON answer",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], share_crew=True)
        with patch.object(type(llm), "call", return_value=response) as call:
            result = crew.kickoff(
                inputs={
                    "client": {"config": {"api_key": SECRET}},
                    "tag": "useful input",
                }
            )
        assert call.call_count > 0
    assert json.loads(result.raw) == json.loads(response)
    return result.raw


def run_tool():
    seen = []

    def lookup(query: str, client: dict) -> str:
        """Return a local synthetic result."""
        seen.append((query, client))
        return json.dumps(
            {"api_key": SECRET, "answer": "useful tool answer", "total_tokens": 7}
        )

    if FLOOR:
        from crewai.agents.tools_handler import ToolsHandler
        from langchain_core.tools import StructuredTool

        tool = StructuredTool.from_function(lookup)
        usage = ToolUsage(
            tools_handler=ToolsHandler(),
            tools=[tool],
            original_tools=[],
            tools_description="lookup",
            tools_names="lookup",
            task=SimpleNamespace(used_tools=0),
            function_calling_llm=None,
            agent=SimpleNamespace(tools_results=[]),
            action=None,
        )
        usage._printer.print = lambda **kwargs: None
    else:
        from crewai.tools.structured_tool import CrewStructuredTool

        tool = CrewStructuredTool.from_function(lookup)
        usage = ToolUsage(
            tools_handler=None, tools=[tool], task=None, function_calling_llm=None
        )
    arguments = {"query": "useful query", "client": {"config": {"api_key": SECRET}}}
    calling = ToolCalling(tool_name="lookup", arguments=arguments)
    result = usage.use(calling, "lookup")
    assert seen == [("useful query", {"config": {"api_key": SECRET}})]
    assert (
        calling.arguments == arguments
        and calling.arguments["client"]["config"]["api_key"] == SECRET
    )
    assert json.loads(result) == {
        "api_key": SECRET,
        "answer": "useful tool answer",
        "total_tokens": 7,
    }
    return result


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"hide_inputs": True},
        {"hide_outputs": True},
        {"hide_input_text": True, "hide_output_messages": True},
    ],
)
def test_native_instrumented_and_uninstrumented_outcomes_match(settings, caplog):
    baseline_crew, baseline_tool = run_crew(), run_tool()
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CrewAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**settings))
    try:
        assert run_crew() == baseline_crew
        assert run_tool() == baseline_tool
    finally:
        instrumentor.uninstrument()
    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} >= {
        "Task._execute_core",
        "Crew.kickoff",
        "ToolUsage._use",
    }
    assert not any("telemetry" in span.name.lower() for span in spans)
    wire = encode_spans(spans).SerializeToString()
    assert SECRET.encode() not in wire and SECRET not in caplog.text
    if not settings:
        assert all(
            text in wire
            for text in (
                b"useful query",
                b"useful tool answer",
                b"useful input",
                b"Return useful answer",
            )
        )
    provider.shutdown()
