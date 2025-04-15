import asyncio
import base64
import os

import anthropic
import httpx
from anthropic import Anthropic, AsyncAnthropic
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_anthropic import AnthropicInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.LLM,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct {{query}}",
        },
        custom_eval_name="<custom_eval_name_1>",
    ),
    EvalTag(
        eval_name=EvalName.CONTEXT_ADHERENCE,
        value=EvalSpanKind.LLM,
        type=EvalTagType.OBSERVATION_SPAN,
        config={},
        mapping={
            "output": "llm.output_messages.0.message.content",
            "context": "llm.input_messages.1.message.content",
        },
        custom_eval_name="<custom_eval_name_2>",
    ),
]

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the Anthropic instrumentation
AnthropicInstrumentor().instrument(tracer_provider=trace_provider)

image_url = "https://upload.wikimedia.org/wikipedia/commons/a/a7/Camponotus_flavomarginatus_ant.jpg"
image_media_type = "image/jpeg"
image_data = base64.standard_b64encode(httpx.get(image_url).content).decode("utf-8")


# Synchronous message example
def sync_message_example():
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        max_tokens=1024,
        # system="this is a system prompt",
        system=[
            {"type": "text", "text": "You are a helpful AI assistant."},
            {"type": "text", "text": "Focus on scientific topics."},
        ],
        messages=[
            {"role": "user", "content": "Hello, Claude"},
            {"role": "user", "content": "Can you explain what language models are?"},
            {
                "role": "user",
                "content": [
                    # {
                    #     "type": "image",
                    #     "source": {
                    #         "type": "base64",
                    #         "media_type": image_media_type,
                    #         "data": image_data,
                    #     },
                    # },
                    {
                        "type": "text",
                        "text": "This image shows an ant, get an extreme close-up perspective.",
                    },
                    {
                        "type": "text",
                        "text": "This image shows an ant, get an extreme close-up perspective.",
                    },
                ],
            },
        ],
        model="claude-3-5-sonnet-latest",
    )
    print("Sync message content:", message.content)


# Asynchronous message example
async def async_message_example():
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = await client.messages.create(
        max_tokens=1024,
        messages=[
            {"role": "user", "content": "Hello, Claude"},
            {"role": "user", "content": "Can you explain what language models are?"},
        ],
        model="claude-3-5-sonnet-latest",
    )
    print("Async message content:", message.content)


# Asynchronous completions example
async def async_completions_example():
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = await client.messages.create(
        max_tokens=1024,
        messages=[
            {"role": "user", "content": "Hello, Claude"},
            {"role": "user", "content": "Once upon a time..."},
        ],
        model="claude-3-5-sonnet-latest",
    )
    print("Async completion content:", message.content)


# Tools example
def tools_example():
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        max_tokens=1024,
        messages=[
            {"role": "user", "content": "Hello, Claude"},
            {"role": "user", "content": "What's the weather like in San Francisco?"},
        ],
        model="claude-3-5-sonnet-latest",
        tools=[
            {
                "name": "get_weather",
                "description": "Get the current weather in a given location",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city and state, e.g. San Francisco, CA",
                        },
                        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                    },
                    "required": ["location"],
                },
            }
        ],
    )
    print("Tool response:", message.content)


# Asynchronous stream example
async def async_stream_example():
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    stream = await client.messages.create(
        max_tokens=1024,
        messages=[
            {"role": "user", "content": "Hello, Claude"},
            {"role": "user", "content": "What's the weather like in San Francisco?"},
        ],
        model="claude-3-5-sonnet-latest",
        stream=True,
    )
    async for event in stream:
        print("Stream event type:", event.type)


# Message stream example
async def message_stream_example():
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    stream = await client.messages.create(
        max_tokens=1024,
        messages=[
            {"role": "user", "content": "Hello, Claude"},
            {"role": "user", "content": "What's the weather like in San Francisco?"},
        ],
        model="claude-3-5-sonnet-latest",
        stream=True,
    )
    async for event in stream:
        print(event)


# Token counting example
def token_counting_example():
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    response = client.messages.count_tokens(
        model="claude-3-5-sonnet-latest",
        messages=[
            {"role": "user", "content": "Hello, world"},
            {"role": "user", "content": "What's the weather like in San Francisco?"},
        ],
    )
    print(f"Input tokens: {response.input_tokens}")


# Multi-turn conversation example
def multi_turn_conversation_example():
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model="claude-3-5-sonnet-latest",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": "Hello, Claude"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Can you describe LLMs to me?"},
        ],
    )
    print("Multi-turn conversation response:", message.content)


# Multiple choice example
def multiple_choice_example():
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model="claude-3-5-sonnet-latest",
        max_tokens=1,
        messages=[
            {
                "role": "user",
                "content": "What is latin for Ant? (A) Apoidea, (B) Rhopalocera, (C) Formicidae",
            },
            {"role": "assistant", "content": "The answer is ("},
        ],
    )
    print("Multiple choice response:", message.content)


def retriever_example():
    from duckduckgo_search import DDGS

    # Initialize DuckDuckGo search
    ddg = DDGS()

    # Create Anthropic client
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # Example query
    query = "Who won the 2023 Women's World Cup?"

    # Perform search
    search_results = list(ddg.text(query, max_results=3))
    search_context = "\n".join([result["body"] for result in search_results])

    # Create prompt with search results
    prompt = f"""Here are some search results about the query: "{query}"

Search context:
{search_context}

Based on these search results, please answer the query."""

    # Get response from Claude
    message = client.messages.create(
        model="claude-3-5-sonnet-latest",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    print("Retriever response:", message.content)


sync_message_example()
asyncio.run(async_message_example())
asyncio.run(async_completions_example())
tools_example()
asyncio.run(async_stream_example())
asyncio.run(message_stream_example())
token_counting_example()
multi_turn_conversation_example()
multiple_choice_example()
retriever_example()
