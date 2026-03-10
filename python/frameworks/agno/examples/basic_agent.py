"""Basic Agno agent example with TraceAI instrumentation.

This example demonstrates how to set up tracing for a simple Agno agent.
"""

import os

# Setup tracing BEFORE importing Agno
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_agno import configure_agno_tracing

# Initialize TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="agno-basic-example",
)

# Configure Agno to use TraceAI
configure_agno_tracing(tracer_provider=trace_provider)

# Now import Agno modules
from agno.agent import Agent
from agno.models.openai import OpenAIChat


def main():
    """Run a basic agent example."""
    # Create an agent with OpenAI model
    agent = Agent(
        name="BasicAssistant",
        model=OpenAIChat(id="gpt-4"),
        description="A helpful assistant that answers questions clearly and concisely.",
        instructions=[
            "Be helpful and accurate",
            "Provide concise answers",
            "Ask for clarification if needed",
        ],
        markdown=True,
    )

    # Run the agent with a simple query
    print("Agent: BasicAssistant")
    print("-" * 40)

    response = agent.run("What is the capital of France?")
    print(f"Response: {response.content}")

    # Run another query
    print()
    response = agent.run("What are three interesting facts about Paris?")
    print(f"Response: {response.content}")


if __name__ == "__main__":
    main()
