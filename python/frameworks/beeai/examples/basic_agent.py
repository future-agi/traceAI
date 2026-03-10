"""Basic BeeAI Agent example with TraceAI observability.

This example demonstrates:
- Setting up TraceAI with BeeAI Framework
- Creating a simple agent with tracing
- Making agent calls with automatic trace export

IMPORTANT: Call configure_beeai_tracing() BEFORE importing BeeAI modules!
"""

# Setup TraceAI FIRST (before importing beeai)
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_beeai import configure_beeai_tracing

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="beeai-basic-agent",
)

# Configure BeeAI to use TraceAI
configure_beeai_tracing(tracer_provider=trace_provider)

# NOW import BeeAI modules
from beeai_framework.agents import Agent
from beeai_framework.backend.chat import ChatModel
from beeai_framework.memory import UnconstrainedMemory


def main():
    # Create model (using IBM Granite via Ollama for local development)
    model = ChatModel.from_name(
        "ollama:granite3.1-dense:8b",
        # Or use other providers:
        # "openai:gpt-4",
        # "watsonx:ibm/granite-3-8b-instruct",
    )

    # Create agent
    agent = Agent(
        llm=model,
        role="Assistant",
        instructions="You are a helpful AI assistant that provides concise answers.",
        memory=UnconstrainedMemory(),
    )

    # Have a conversation
    questions = [
        "What is the capital of France?",
        "What's the population there?",
        "What's a famous landmark?",
    ]

    print("=" * 60)
    print("Basic BeeAI Agent Demo with TraceAI")
    print("=" * 60)

    for question in questions:
        print(f"\nUser: {question}")
        response = agent.run(question)
        print(f"Agent: {response.output}")

    print("\n" + "=" * 60)
    print("Traces have been sent to TraceAI!")
    print("View them in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
