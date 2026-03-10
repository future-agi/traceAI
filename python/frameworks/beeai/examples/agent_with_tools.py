"""BeeAI Agent with tools example with TraceAI observability.

This example demonstrates:
- Creating agents with tools
- Using built-in BeeAI tools
- Tracing tool execution
"""

# Setup TraceAI FIRST (before importing beeai)
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_beeai import configure_beeai_tracing

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="beeai-tools-agent",
)

configure_beeai_tracing(tracer_provider=trace_provider)

# NOW import BeeAI modules
from beeai_framework.agents import Agent
from beeai_framework.backend.chat import ChatModel
from beeai_framework.tools import WikipediaTool, OpenMeteoTool, ThinkTool
from beeai_framework.memory import UnconstrainedMemory


def main():
    # Create model
    model = ChatModel.from_name("ollama:granite3.1-dense:8b")

    # Create agent with tools
    agent = Agent(
        llm=model,
        role="Research Assistant",
        instructions="""You are a research assistant with access to tools.
        Use Wikipedia for factual information and weather tools for forecasts.
        Always verify information using available tools.""",
        tools=[
            ThinkTool(),       # Internal reasoning
            WikipediaTool(),   # Knowledge retrieval
            OpenMeteoTool(),   # Weather forecasts
        ],
        memory=UnconstrainedMemory(),
    )

    # Test queries that require different tools
    queries = [
        "What is the Eiffel Tower and when was it built?",
        "What's the weather forecast for Paris tomorrow?",
        "Tell me about the history of the Louvre Museum.",
    ]

    print("=" * 60)
    print("BeeAI Agent with Tools Demo")
    print("=" * 60)
    print("\nAvailable tools: ThinkTool, WikipediaTool, OpenMeteoTool")

    for query in queries:
        print(f"\n{'-' * 60}")
        print(f"User: {query}")
        response = agent.run(query)
        print(f"Agent: {response.output}")

    print("\n" + "=" * 60)
    print("All tool executions have been traced!")
    print("View the traces in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
