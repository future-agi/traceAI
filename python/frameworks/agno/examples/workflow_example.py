"""Trace a workflow using Agno native agent steps.

Set FI_API_KEY, FI_SECRET_KEY, and OPENAI_API_KEY before running.
"""

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import SemanticConvention
from openinference.instrumentation import TraceConfig
from traceai_agno import configure_agno_tracing
from agno.workflow import Workflow


def main():
    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="agno-workflow-example",
        semantic_convention=SemanticConvention.OPENINFERENCE,
    )
    tracing = configure_agno_tracing(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    try:
        researcher = Agent(name="Researcher", model=OpenAIChat(id="gpt-4o-mini"), telemetry=False)
        summarizer = Agent(
            name="Summarizer", model=OpenAIChat(id="gpt-4o-mini"),
            instructions=["Summarize the preceding result in one sentence."], telemetry=False,
        )
        workflow = Workflow(name="Research workflow", steps=[researcher, summarizer], telemetry=False)
        print(workflow.run("What is the capital of France?").content)
        provider.force_flush()
    finally:
        tracing.uninstrument()
        provider.shutdown()


if __name__ == "__main__":
    main()
