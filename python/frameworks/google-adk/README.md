# TraceAI Google ADK Instrumentation

[![pypi](https://badge.fury.io/py/traceai-google-adk.svg)](https://pypi.org/project/traceai-google-adk/)

Python auto-instrumentation library for Google ADK.


## Quickstart

In this example we will instrument a small program that uses Gemini and observe the traces in Future AGI Dashboard

Install packages.

```shell
pip install traceai-google-adk
```

In a python file, set up the `GoogleADKInstrumentor` and configure the tracer to send traces to Observe.

```python
import asyncio

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from traceai_google_adk import GoogleADKInstrumentor

tracer_provider = register(
    project_name="test_project",
    project_type=ProjectType.OBSERVE,
)


GoogleADKInstrumentor().instrument(tracer_provider=tracer_provider)


def get_weather(city: str) -> dict:
    """Retrieves the current weather report for a specified city.

    Args:
        city (str): The name of the city for which to retrieve the weather report.

    Returns:
        dict: status and result or error msg.
    """
    if city.lower() == "new york":
        return {
            "status": "success",
            "report": (
                "The weather in New York is sunny with a temperature of 25 degrees"
                " Celsius (77 degrees Fahrenheit)."
            ),
        }
    else:
        return {
            "status": "error",
            "error_message": f"Weather information for '{city}' is not available.",
        }


agent = Agent(
    name="test_agent",
    model="gemini-2.5-flash-preview-05-20",
    description="Agent to answer questions using tools.",
    instruction="You must use the available tools to find an answer.",
    tools=[get_weather],
)


async def main():
    app_name = "test_instrumentation"
    user_id = "test_user"
    session_id = "test_session"
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    session_service = runner.session_service
    await session_service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=[types.Part(text="What is the weather in New York?")]
        ),
    ):
        if event.is_final_response():
            print(event.content.parts[0].text.strip())


if __name__ == "__main__":
    asyncio.run(main())

```

Since we are using Gemini, we must set the `GOOGLE_API_KEY` environment variable to authenticate with the Gemini API.

```shell
export GOOGLE_API_KEY=your-api-key
```

Run the python file to send the traces to Future AGI Platform.

```shell
python your_file.py
```