"""BeeAI Agent with custom middleware tracing example.

This example demonstrates:
- Using TraceAIMiddleware for extended tracing
- Session and user tracking
- Custom event capture
"""

# Setup TraceAI FIRST
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_beeai import configure_beeai_tracing, create_tracing_middleware

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="beeai-middleware-demo",
)

configure_beeai_tracing(tracer_provider=trace_provider)

# NOW import BeeAI modules
from beeai_framework.agents import Agent
from beeai_framework.backend.chat import ChatModel
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.tools import ThinkTool


def main():
    # Create custom middleware with session tracking
    middleware = create_tracing_middleware(
        tracer_provider=trace_provider,
        capture_input=True,
        capture_output=True,
        session_id="user-session-12345",
        user_id="demo@example.com",
    )

    # Create model
    model = ChatModel.from_name("ollama:granite3.1-dense:8b")

    # Create agent with middleware
    agent = Agent(
        llm=model,
        role="Assistant",
        instructions="You are a helpful AI assistant.",
        tools=[ThinkTool()],
        memory=UnconstrainedMemory(),
        middlewares=[middleware],
    )

    print("=" * 60)
    print("BeeAI Middleware Tracing Demo")
    print("=" * 60)
    print("\nSession ID: user-session-12345")
    print("User ID: demo@example.com")

    # Simulate conversation with middleware tracking
    prompts = [
        "Hello! What can you help me with?",
        "Explain how neural networks work.",
        "Can you summarize that in one sentence?",
    ]

    for prompt in prompts:
        print(f"\n{'-' * 60}")
        print(f"User: {prompt}")

        # Middleware events are triggered automatically by BeeAI
        response = agent.run(prompt)

        print(f"Agent: {response.output}")

    print("\n" + "=" * 60)
    print("All interactions traced with session/user context!")
    print("View the traces in your TraceAI dashboard.")
    print("=" * 60)


if __name__ == "__main__":
    main()
