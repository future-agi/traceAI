"""
E2E Tests for CrewAI SDK Instrumentation

Tests CrewAI instrumentation using Google's OpenAI-compatible endpoint.
"""

import pytest
import os
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_crewai():
    """Set up CrewAI with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_crewai import CrewAIInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_crewai not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    CrewAIInstrumentor().instrument(tracer_provider=tracer_provider)

    # Set env vars for CrewAI LLM config
    os.environ["OPENAI_API_BASE"] = config.google_openai_base_url
    os.environ["OPENAI_API_KEY"] = config.google_api_key

    yield

    CrewAIInstrumentor().uninstrument()


@skip_if_no_google
class TestCrewAIExecution:
    """Test CrewAI execution."""

    def test_simple_crew(self, setup_crewai):
        """Test a simple crew with one agent and one task."""
        from crewai import Agent, Task, Crew

        agent = Agent(
            role="Helpful Assistant",
            goal="Answer questions briefly and accurately",
            backstory="You are a helpful AI assistant.",
            llm=f"openai/{config.google_model}",
            verbose=False,
        )

        task = Task(
            description="What is 2+2? Answer with just the number.",
            expected_output="A single number",
            agent=agent,
        )

        crew = Crew(
            agents=[agent],
            tasks=[task],
            verbose=False,
        )

        result = crew.kickoff()

        assert result is not None
        assert "4" in str(result)
        time.sleep(2)
        print(f"Crew result: {result}")

    def test_multi_agent_crew(self, setup_crewai):
        """Test crew with multiple agents."""
        from crewai import Agent, Task, Crew

        researcher = Agent(
            role="Researcher",
            goal="Research topics and provide factual information",
            backstory="You are a research assistant.",
            llm=f"openai/{config.google_model}",
            verbose=False,
        )

        writer = Agent(
            role="Writer",
            goal="Write clear and concise summaries",
            backstory="You are a technical writer.",
            llm=f"openai/{config.google_model}",
            verbose=False,
        )

        research_task = Task(
            description="What is the capital of France? Provide just the city name.",
            expected_output="City name",
            agent=researcher,
        )

        write_task = Task(
            description="Take the research result and write a one-sentence summary about this city.",
            expected_output="A one-sentence summary",
            agent=writer,
        )

        crew = Crew(
            agents=[researcher, writer],
            tasks=[research_task, write_task],
            verbose=False,
        )

        result = crew.kickoff()

        assert result is not None
        assert "Paris" in str(result)
        print(f"Multi-agent result: {result}")

    def test_crew_with_tool(self, setup_crewai):
        """Test crew with custom tool."""
        from crewai import Agent, Task, Crew
        from crewai.tools import BaseTool

        class AddTool(BaseTool):
            name: str = "add_numbers"
            description: str = "Add two numbers together. Input: two numbers separated by comma."

            def _run(self, input_str: str) -> str:
                try:
                    parts = input_str.split(",")
                    a, b = int(parts[0].strip()), int(parts[1].strip())
                    return str(a + b)
                except Exception:
                    return "Error: provide two numbers separated by comma"

        agent = Agent(
            role="Calculator",
            goal="Use tools to perform calculations",
            backstory="You are a math assistant that uses tools.",
            llm=f"openai/{config.google_model}",
            tools=[AddTool()],
            verbose=False,
        )

        task = Task(
            description="What is 15 + 27? Use the add_numbers tool.",
            expected_output="The sum of 15 and 27",
            agent=agent,
        )

        crew = Crew(agents=[agent], tasks=[task], verbose=False)
        result = crew.kickoff()

        assert result is not None
        assert "42" in str(result)
        print(f"Tool result: {result}")
