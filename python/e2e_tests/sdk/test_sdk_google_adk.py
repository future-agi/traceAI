"""
E2E Tests for Google ADK (Agent Development Kit) Instrumentation

Tests Google ADK instrumentation using GOOGLE_API_KEY directly.
"""

import pytest
import time

from config import config, skip_if_no_google


@pytest.fixture(scope="module")
def setup_google_adk():
    """Set up Google ADK with instrumentation."""
    if not config.has_google():
        pytest.skip("GOOGLE_API_KEY not set")

    from fi_instrumentation import register
    try:
        from traceai_google_adk import GoogleADKInstrumentor
    except (ImportError, AttributeError):
        pytest.skip("traceai_google_adk not installed or incompatible")

    tracer_provider = register(
        project_name=config.project_name,
        project_version_name=config.project_version_name,
        project_type=config.project_type,
        verbose=False,
    )

    GoogleADKInstrumentor().instrument(tracer_provider=tracer_provider)

    yield

    GoogleADKInstrumentor().uninstrument()


@skip_if_no_google
class TestGoogleADKAgent:
    """Test Google ADK agent operations."""

    def test_simple_agent(self, setup_google_adk):
        """Test simple ADK agent."""
        import os
        os.environ["GOOGLE_API_KEY"] = config.google_api_key

        from google.adk.agents import Agent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        agent = Agent(
            name="assistant",
            model=config.google_model,
            instruction="You are a helpful assistant. Answer briefly.",
        )

        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="e2e_test", session_service=session_service)

        session = session_service.create_session(app_name="e2e_test", user_id="test_user")

        from google.genai import types

        user_message = types.Content(
            role="user",
            parts=[types.Part(text="What is 2+2?")],
        )

        final_response = None
        for event in runner.run(
            user_id="test_user",
            session_id=session.id,
            new_message=user_message,
        ):
            if event.is_final_response():
                final_response = event.content

        assert final_response is not None
        response_text = final_response.parts[0].text
        assert "4" in response_text
        time.sleep(2)
        print(f"ADK Agent result: {response_text}")

    def test_agent_with_tool(self, setup_google_adk):
        """Test ADK agent with tool."""
        import os
        os.environ["GOOGLE_API_KEY"] = config.google_api_key

        from google.adk.agents import Agent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.adk.tools import FunctionTool
        from google.genai import types

        def get_weather(location: str) -> str:
            """Get weather for a location.

            Args:
                location: City name

            Returns:
                Weather description
            """
            return f"Sunny and 72F in {location}"

        agent = Agent(
            name="weather_assistant",
            model=config.google_model,
            instruction="You are a weather assistant. Use tools to get weather info.",
            tools=[get_weather],
        )

        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="e2e_test_tool", session_service=session_service)

        session = session_service.create_session(app_name="e2e_test_tool", user_id="test_user")

        user_message = types.Content(
            role="user",
            parts=[types.Part(text="What's the weather in Paris?")],
        )

        final_response = None
        for event in runner.run(
            user_id="test_user",
            session_id=session.id,
            new_message=user_message,
        ):
            if event.is_final_response():
                final_response = event.content

        assert final_response is not None
        response_text = final_response.parts[0].text
        assert "Paris" in response_text or "72" in response_text
        print(f"ADK Tool result: {response_text}")
