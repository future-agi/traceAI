"""Trace a real tool call with an illustrative local weather lookup.

Set FI_API_KEY, FI_SECRET_KEY, and OPENAI_API_KEY before running.
"""

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import SemanticConvention
from openinference.instrumentation import TraceConfig
from traceai_agno import configure_agno_tracing


def weather(city: str) -> str:
    """Look up example weather data for a city (not a live weather service)."""
    return {"paris": "Sunny", "london": "Cloudy"}.get(city.lower(), "No example data")


def main():
    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="agno-agent-with-tools",
        semantic_convention=SemanticConvention.OPENINFERENCE,
    )
    tracing = configure_agno_tracing(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    try:
        agent = Agent(
            name="Weather assistant",
            model=OpenAIChat(id="gpt-4o-mini"),
            tools=[weather],
            instructions=["Use the weather tool to answer weather questions."],
            telemetry=False,
        )
        print(agent.run("What is the weather in Paris?").content)
        provider.force_flush()
    finally:
        tracing.uninstrument()
        provider.shutdown()


if __name__ == "__main__":
    main()
