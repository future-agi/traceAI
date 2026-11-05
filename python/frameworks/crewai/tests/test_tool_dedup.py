"""Legacy delegation must not double-count modern tool entry points."""

from crewai.tools.tool_calling import ToolCalling
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_crewai import CrewAIInstrumentor


def test_legacy_tool_delegation_has_one_span(native_tool_usage):
    calls = []

    def lookup(query: str) -> str:
        """Return a synthetic answer."""
        calls.append(query)
        return "useful result"

    usage = native_tool_usage(lookup)
    provider = TracerProvider(shutdown_on_exit=False)
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    inst = CrewAIInstrumentor()
    inst.instrument(tracer_provider=provider)
    try:
        with provider.get_tracer(__name__).start_as_current_span("parent") as parent:
            result = usage.use(
                ToolCalling(tool_name="lookup", arguments={"query": "positive"}),
                "lookup",
            )
            assert result == "useful result" and calls == ["positive"]
            spans = memory.get_finished_spans()
            assert len(spans) == 1
            assert spans[0].name == "ToolUsage._use"
            assert spans[0].parent.span_id == parent.get_span_context().span_id
    finally:
        inst.uninstrument()
        provider.shutdown()
