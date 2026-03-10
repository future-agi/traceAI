"""AutoGen v0.4 SelectorGroupChat example with tracing.

This example demonstrates:
- Creating a team with dynamic agent selection
- Using SelectorGroupChat for intelligent routing
- Tracing selector decisions and agent interactions
"""

import asyncio
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_autogen import instrument_autogen

# Setup tracing
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen-team-selector",
)
instrument_autogen(tracer_provider=trace_provider)

# Import AutoGen v0.4
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_ext.models.openai import OpenAIChatCompletionClient


async def main():
    # Create model client
    model = OpenAIChatCompletionClient(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    # Create specialized agents for a software development team
    architect = AssistantAgent(
        name="architect",
        description="Software architect who designs system architecture and high-level structure.",
        model_client=model,
        system_message="""You are a software architect.
        Focus on:
        - System design and architecture
        - Technology stack decisions
        - Scalability considerations
        - Design patterns
        Always start by understanding requirements before proposing solutions.""",
    )

    backend_dev = AssistantAgent(
        name="backend_developer",
        description="Backend developer who implements server-side logic and APIs.",
        model_client=model,
        system_message="""You are a senior backend developer.
        Focus on:
        - API design and implementation
        - Database schema design
        - Server-side business logic
        - Performance optimization
        Write production-ready Python code.""",
    )

    frontend_dev = AssistantAgent(
        name="frontend_developer",
        description="Frontend developer who builds user interfaces and client-side features.",
        model_client=model,
        system_message="""You are a senior frontend developer.
        Focus on:
        - User interface design
        - React/TypeScript implementation
        - User experience optimization
        - Responsive design
        Write clean, accessible code.""",
    )

    devops = AssistantAgent(
        name="devops_engineer",
        description="DevOps engineer who handles deployment, CI/CD, and infrastructure.",
        model_client=model,
        system_message="""You are a DevOps engineer.
        Focus on:
        - CI/CD pipeline design
        - Container orchestration (Docker, Kubernetes)
        - Infrastructure as Code
        - Monitoring and observability
        Say DEPLOYED when infrastructure is ready.""",
    )

    # Create selector-based team
    # The selector model decides which agent should respond next
    team = SelectorGroupChat(
        participants=[architect, backend_dev, frontend_dev, devops],
        model_client=model,  # Used for agent selection
        termination_condition=MaxMessageTermination(max_messages=10)
        | TextMentionTermination("DEPLOYED"),
    )

    # Run the team task
    print("Starting software development team...")
    print("Task: Design and implement a TODO API")
    print("-" * 50)

    result = await team.run(
        task="""Design and implement a REST API for a TODO list application.

        Requirements:
        1. CRUD operations for TODO items
        2. User authentication
        3. Due date and priority support
        4. Deployment to cloud

        Please coordinate as a team to deliver this."""
    )

    print("-" * 50)
    print("\n--- Team Task Complete ---")
    print(f"Total messages: {len(result.messages)}")
    print(f"Stop reason: {result.stop_reason}")

    # Analyze which agents participated
    agent_messages = {}
    for msg in result.messages:
        source = getattr(msg, "source", "unknown")
        agent_messages[source] = agent_messages.get(source, 0) + 1

    print("\n--- Agent Participation ---")
    for agent, count in sorted(agent_messages.items(), key=lambda x: -x[1]):
        print(f"  {agent}: {count} messages")


if __name__ == "__main__":
    asyncio.run(main())
