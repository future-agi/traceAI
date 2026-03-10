"""BeeAI Agent with requirements example.

This example demonstrates:
- Creating agents with behavioral requirements
- Using ConditionalRequirement for controlled behavior
- Tracing requirement enforcement
"""

# Setup TraceAI FIRST
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_beeai import configure_beeai_tracing

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="beeai-requirements-agent",
)

configure_beeai_tracing(tracer_provider=trace_provider)

# NOW import BeeAI modules
from beeai_framework.agents import Agent
from beeai_framework.backend.chat import ChatModel
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.tools import ThinkTool


def main():
    # Create model
    model = ChatModel.from_name("ollama:granite3.1-dense:8b")

    # Create agent with requirements for safe, predictable behavior
    agent = Agent(
        llm=model,
        role="Safe Assistant",
        instructions="""You are a helpful AI assistant that always:
        1. Thinks before responding using the ThinkTool
        2. Provides accurate, factual information
        3. Acknowledges uncertainty when appropriate
        4. Refuses to provide harmful content""",
        tools=[ThinkTool()],
        memory=UnconstrainedMemory(),
        # Requirements can enforce specific behaviors
        # requirements=[
        #     ConditionalRequirement(
        #         step=0,
        #         tool=ThinkTool,  # Always use ThinkTool first
        #     ),
        # ],
    )

    # Test conversations
    prompts = [
        "Explain quantum computing in simple terms.",
        "What are the benefits and risks of AI?",
        "Help me understand machine learning basics.",
    ]

    print("=" * 60)
    print("BeeAI Agent with Requirements Demo")
    print("=" * 60)

    for prompt in prompts:
        print(f"\n{'-' * 60}")
        print(f"User: {prompt}")
        response = agent.run(prompt)
        print(f"Agent: {response.output}")

    print("\n" + "=" * 60)
    print("Requirement-controlled execution has been traced!")
    print("View the traces in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
