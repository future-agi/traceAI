"""Run a basic Agno agent with Future AGI tracing.

Set FI_API_KEY, FI_SECRET_KEY, and OPENAI_API_KEY before running.
"""

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import SemanticConvention
from openinference.instrumentation import TraceConfig
from traceai_agno import configure_agno_tracing


def main():
    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="agno-basic-agent",
        semantic_convention=SemanticConvention.OPENINFERENCE,
    )
    tracing = configure_agno_tracing(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    try:
        agent = Agent(name="Assistant", model=OpenAIChat(id="gpt-4o-mini"), telemetry=False)
        print(agent.run("What is the capital of France?").content)
        provider.force_flush()
    finally:
        tracing.uninstrument()
        provider.shutdown()


if __name__ == "__main__":
    main()
