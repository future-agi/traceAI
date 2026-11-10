"""Trace a Semantic Kernel ChatCompletionAgent with Future AGI. No model key needed.

The model is mocked: Semantic Kernel's real OpenAI connector talks to an
in-process ``httpx.MockTransport``, so no request leaves the machine for the
model. Spans are exported with ``fi_instrumentation.register()``; set
``FI_API_KEY`` and ``FI_SECRET_KEY`` (and optionally ``FI_PROJECT_NAME``,
``FI_BASE_URL``) to see the trace in Future AGI. Without keys the export fails
and is logged; the agent still answers.

    pip install traceAI-semantic-kernel
    python examples/basic_agent.py

You do not export any SEMANTICKERNEL_* variable: ``instrument()`` turns Semantic
Kernel's diagnostics on in process. Message bodies stay off (``sensitive=False``).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated

import httpx
from openai import AsyncOpenAI

from fi_instrumentation import register, using_session
from fi_instrumentation.fi_types import ProjectType
from traceai_semantic_kernel import SemanticKernelInstrumentor

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name=os.getenv("FI_PROJECT_NAME", "semantic-kernel-example"),
    verbose=False,
)
# Enables Semantic Kernel's native diagnostics. Does not wrap Kernel.invoke.
# sensitive=False is the default; True stores prompts and tool results.
SemanticKernelInstrumentor().instrument(tracer_provider=trace_provider, sensitive=False)

from semantic_kernel import Kernel  # noqa: E402
from semantic_kernel.agents import ChatCompletionAgent  # noqa: E402
from semantic_kernel.connectors.ai.function_choice_behavior import FunctionChoiceBehavior  # noqa: E402
from semantic_kernel.connectors.ai.open_ai import (  # noqa: E402
    OpenAIChatCompletion,
    OpenAIChatPromptExecutionSettings,
)
from semantic_kernel.functions import KernelArguments, kernel_function  # noqa: E402


class WeatherPlugin:
    @kernel_function(name="get_weather", description="Current weather for a city")
    def get_weather(self, city: Annotated[str, "City name"]) -> str:
        return "sunny, 21 C in " + city


def mock_chat_completions(request: httpx.Request) -> httpx.Response:
    """A stand-in for the OpenAI chat completions endpoint: one tool call, then an answer."""
    body = json.loads(request.content or b"{}")
    messages = body.get("messages") or []
    if body.get("tools") and messages and messages[-1].get("role") != "tool":
        tool = body["tools"][0]["function"]["name"]
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_mock_1",
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps({"city": "Paris"})},
                }
            ],
        }
        finish = "tool_calls"
    else:
        tool_result = messages[-1].get("content") if messages else ""
        message = {"role": "assistant", "content": "It is {0}.".format(tool_result)}
        finish = "stop"
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": body.get("model", "gpt-4o-mini"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        },
    )


async def main() -> None:
    client = AsyncOpenAI(
        api_key="mock-key-not-used",
        base_url="http://mock-openai.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_chat_completions)),
    )
    kernel = Kernel()
    kernel.add_service(OpenAIChatCompletion(ai_model_id="gpt-4o-mini", async_client=client))
    kernel.add_plugin(WeatherPlugin(), "Weather")

    agent = ChatCompletionAgent(
        kernel=kernel,
        name="WeatherAgent",
        instructions="Answer weather questions with the Weather plugin.",
        arguments=KernelArguments(
            settings=OpenAIChatPromptExecutionSettings(function_choice_behavior=FunctionChoiceBehavior.Auto())
        ),
    )
    # Semantic Kernel does not put a thread id on its spans; set the session yourself.
    with using_session("example-session-1"):
        response = await agent.get_response("What is the weather in Paris?")
    print(response.message.content)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # Short scripts must flush; the batch exporter sends in the background.
        trace_provider.force_flush()
        trace_provider.shutdown()
