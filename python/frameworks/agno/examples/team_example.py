"""Agno multi-agent team example with TraceAI instrumentation.

This example demonstrates how to set up tracing for a team of agents
working together to accomplish complex tasks.
"""

import os

# Setup tracing BEFORE importing Agno
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_agno import configure_agno_tracing, get_team_attributes

# Initialize TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="agno-team-example",
)

# Configure Agno to use TraceAI
configure_agno_tracing(tracer_provider=trace_provider)

# Now import Agno modules
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.team import Team


def main():
    """Run a multi-agent team example."""
    # Create specialized agents
    researcher = Agent(
        name="Researcher",
        model=OpenAIChat(id="gpt-4"),
        description="A research specialist that gathers and analyzes information.",
        instructions=[
            "Research topics thoroughly",
            "Provide factual, well-sourced information",
            "Summarize findings clearly",
        ],
    )

    writer = Agent(
        name="Writer",
        model=OpenAIChat(id="gpt-4"),
        description="A content writer that creates clear, engaging text.",
        instructions=[
            "Write in a clear, engaging style",
            "Structure content logically",
            "Use appropriate formatting",
        ],
    )

    editor = Agent(
        name="Editor",
        model=OpenAIChat(id="gpt-4"),
        description="An editor that reviews and improves content quality.",
        instructions=[
            "Check for clarity and accuracy",
            "Improve grammar and style",
            "Ensure consistent tone",
        ],
    )

    # Create a team with the agents
    content_team = Team(
        name="ContentTeam",
        agents=[researcher, writer, editor],
        description="A team that researches, writes, and edits content collaboratively.",
    )

    # Log team attributes
    team_attrs = get_team_attributes(content_team)
    print("Team Configuration:")
    print(f"  Name: {team_attrs.get('agno.team.name')}")
    print(f"  Members: {team_attrs.get('agno.team.members')}")
    print("=" * 50)

    # Run the team on a task
    task = "Write a short article about the benefits of AI in healthcare."

    print(f"\nTask: {task}")
    print("-" * 40)

    response = content_team.run(task)
    print(f"\nTeam Response:\n{response.content}")


if __name__ == "__main__":
    main()
