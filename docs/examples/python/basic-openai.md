# Basic OpenAI Example

A complete example of tracing OpenAI chat completions.

## Prerequisites

```bash
pip install fi-instrumentation traceai-openai openai
```

## Full Example

```python
import os
from fi_instrumentation import register, using_attributes
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
import openai

# 1. Set environment variables
os.environ["FI_API_KEY"] = "your-api-key"
os.environ["FI_SECRET_KEY"] = "your-secret-key"
os.environ["OPENAI_API_KEY"] = "your-openai-key"

# 2. Register tracer provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="openai_chatbot"
)

# 3. Instrument OpenAI
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

# 4. Create OpenAI client
client = openai.OpenAI()

def chat(user_message: str, session_id: str, user_id: str) -> str:
    """Send a message and get a response."""

    # Add context to the trace
    with using_attributes(
        session_id=session_id,
        user_id=user_id,
        metadata={"source": "chatbot", "version": "1.0"}
    ):
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7,
            max_tokens=500
        )

    return response.choices[0].message.content

# 5. Use the function
if __name__ == "__main__":
    response = chat(
        user_message="What is the capital of France?",
        session_id="session-123",
        user_id="user-456"
    )
    print(f"Response: {response}")
```

## What Gets Captured

The trace will include:

| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `LLM` |
| `llm.system` | `openai` |
| `llm.model_name` | `gpt-4` |
| `llm.input_messages` | System + user messages |
| `llm.output_messages` | Assistant response |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.invocation_parameters` | `{"temperature": 0.7, "max_tokens": 500}` |
| `session.id` | `session-123` |
| `user.id` | `user-456` |
| `metadata` | `{"source": "chatbot", "version": "1.0"}` |

## With Tool Calling

```python
import json

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name"},
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}
                },
                "required": ["location"]
            }
        }
    }
]

def get_weather(location: str, unit: str = "celsius") -> dict:
    """Mock weather function."""
    return {"location": location, "temperature": 22, "unit": unit}

def chat_with_tools(user_message: str) -> str:
    # First call - may request tool use
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": user_message}],
        tools=tools,
        tool_choice="auto"
    )

    message = response.choices[0].message

    # If tool call requested
    if message.tool_calls:
        messages = [
            {"role": "user", "content": user_message},
            message,
        ]

        # Execute each tool call
        for tool_call in message.tool_calls:
            if tool_call.function.name == "get_weather":
                args = json.loads(tool_call.function.arguments)
                result = get_weather(**args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result)
                })

        # Final response with tool results
        final_response = client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            tools=tools
        )

        return final_response.choices[0].message.content

    return message.content

# Use it
response = chat_with_tools("What's the weather in Paris?")
print(response)
```

Tool calls are automatically captured with:
- `llm.tools` - Available tools
- `llm.function_call` - Function call details
- Separate spans for tool execution

## With Streaming

```python
def chat_stream(user_message: str) -> str:
    """Stream the response."""
    stream = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": user_message}],
        stream=True
    )

    full_response = ""
    for chunk in stream:
        if chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            print(content, end="", flush=True)
            full_response += content

    print()  # Newline at end
    return full_response

response = chat_stream("Tell me a short story.")
```

Streaming is fully traced - the span completes when iteration finishes.

## Async Example

```python
import asyncio
from openai import AsyncOpenAI

async def async_chat(user_message: str) -> str:
    """Async chat completion."""
    client = AsyncOpenAI()

    response = await client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": user_message}]
    )

    return response.choices[0].message.content

# Run async
result = asyncio.run(async_chat("Hello!"))
print(result)
```

## With Experiments

Run evaluations on your completions:

```python
from fi_instrumentation.fi_types import (
    EvalTag, EvalTagType, EvalSpanKind, EvalName, ModelChoices
)

eval_tags = [
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.TOXICITY,
        custom_eval_name="output_toxicity",
        mapping={"output": "raw.output"},
        model=ModelChoices.PROTECT_FLASH
    ),
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.IS_HELPFUL,
        custom_eval_name="helpfulness_check",
        mapping={"output": "raw.output"},
        model=ModelChoices.TURING_SMALL
    )
]

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="chatbot_experiment",
    project_version_name="v1.0",
    eval_tags=eval_tags
)

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

## Error Handling

Errors are automatically captured:

```python
try:
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello!"}]
    )
except openai.APIError as e:
    # Error is recorded on the span:
    # - otel.status_code = ERROR
    # - exception.type = APIError
    # - exception.message = error message
    print(f"API error: {e}")
except Exception as e:
    print(f"Error: {e}")
```

## Related

- [Streaming Anthropic](streaming-anthropic.md)
- [LangChain RAG](langchain-rag.md)
- [Context Managers](../../python/context-managers.md)
