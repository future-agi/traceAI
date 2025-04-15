import asyncio
import base64

import httpx
import litellm
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_litellm import LiteLLMInstrumentor

image_url = "https://upload.wikimedia.org/wikipedia/commons/a/a7/Camponotus_flavomarginatus_ant.jpg"
image_media_type = "image/jpeg"
image_data = base64.standard_b64encode(httpx.get(image_url).content).decode("utf-8")

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct",
        },
        custom_eval_name="<custom_eval_name>",
    )
]


def simple_completion():
    return litellm.completion(
        model="gpt-3.5-turbo",
        messages=[
            {
                "content": "What's the capital of China? Get the weather there.",
                "role": "user",
            }
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather in a given location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "The city name",
                            }
                        },
                        "required": ["location"],
                    },
                },
            }
        ],
        tool_choice="auto",
        stream=False,
    )


def conversation_completion():
    return litellm.completion(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {"type": "text", "text": "describe this image?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "What is in this image?"}],
            },
            {
                "role": "user",
                "content": "What is in this image?",
            },
        ],
        temperature=0.7,
    )


async def async_conversation_completion():
    return await litellm.acompletion(
        model="gpt-3.5-turbo",
        messages=[
            {"content": "Hello, I want to bake a cake", "role": "user"},
            {
                "content": "Hello, I can pull up some recipes for cakes.",
                "role": "assistant",
            },
            {"content": "No actually I want to make a pie", "role": "user"},
        ],
        temperature=0.7,
        max_tokens=20,
    )


def completion_with_retry():
    return litellm.completion_with_retries(
        model="gpt-3.5-turbo",
        messages=[{"content": "What's the highest grossing film ever", "role": "user"}],
    )


def embedding_call():
    return litellm.embedding(
        model="text-embedding-ada-002", input=["good morning from litellm"]
    )


async def async_embedding_call():
    return await litellm.aembedding(
        model="text-embedding-ada-002", input=["good morning from litellm"]
    )


def image_generation_call():
    return litellm.image_generation(model="dall-e-2", prompt="cute baby otter")


async def async_image_generation_call():
    return await litellm.aimage_generation(model="dall-e-2", prompt="cute baby otter")


trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    # eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the Lite LLM instrumentor
LiteLLMInstrumentor().instrument(tracer_provider=trace_provider)

simple_completion()
conversation_completion()
completion_with_retry()
embedding_call()
image_generation_call()


async def main():
    await async_conversation_completion()
    await async_embedding_call()
    await async_image_generation_call()


asyncio.run(main())
