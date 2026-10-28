"""Offline AG2 1.x example: a scripted model, traces exported to Future AGI.

Runs without any model API key: ``ag2.testing.TestConfig`` plays the model.
Swap ``config=`` for a real ``ag2.config`` model config (for example
``OpenAIConfig(model=...)``) in your application.

    export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=ag2-demo
    python examples/offline_weather_agent.py
"""

import asyncio
import os

from ag2 import Agent
from ag2.events import ModelMessage, ModelResponse, ToolCallEvent
from ag2.testing import TestConfig
from ag2.usage import Usage
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

from traceai_ag2 import setup


def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"sunny in {city}"


async def main() -> None:
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name=os.getenv("FI_PROJECT_NAME", "ag2-offline-example"),
    )

    agent = Agent(
        "weather_bot",
        "You answer weather questions.",
        config=TestConfig(
            ToolCallEvent("get_weather", arguments='{"city": "Paris"}', id="call_weather_1"),
            ModelResponse(
                ModelMessage("It is sunny in Paris."),
                usage=Usage(prompt_tokens=11, completion_tokens=7, cache_read_input_tokens=3),
                model="fake-model",
                finish_reason="stop",
            ),
        ),
        tools=[get_weather],
    )

    # Attaches AG2's TelemetryMiddleware with capture_content=False.
    setup(agent, tracer_provider=trace_provider)

    reply = await agent.ask("What's the weather in Paris?")
    print(reply.body)

    trace_provider.force_flush()


if __name__ == "__main__":
    asyncio.run(main())
