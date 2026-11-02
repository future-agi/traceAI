"""Trace a team delegating a task to a member agent.

Set FI_API_KEY, FI_SECRET_KEY, and OPENAI_API_KEY before running.
"""

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import SemanticConvention
from openinference.instrumentation import TraceConfig
from traceai_agno import configure_agno_tracing
from agno.team import Team


def main():
    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="agno-team-example",
        semantic_convention=SemanticConvention.OPENINFERENCE,
    )
    tracing = configure_agno_tracing(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    try:
        researcher = Agent(
            id="researcher", name="Researcher",
            model=OpenAIChat(id="gpt-4o-mini"), telemetry=False,
        )
        team = Team(
            name="Research team", members=[researcher],
            model=OpenAIChat(id="gpt-4o-mini"),
            instructions=["Delegate the question to the Researcher before answering."],
            telemetry=False,
        )
        print(team.run("What is the capital of France?").content)
        provider.force_flush()
    finally:
        tracing.uninstrument()
        provider.shutdown()


if __name__ == "__main__":
    main()
