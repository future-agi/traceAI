# Streaming Anthropic Example

Trace streaming responses with Anthropic Claude.

## Prerequisites

```bash
pip install fi-instrumentation traceai-anthropic anthropic
```

## Full Example

```python
import os
from fi_instrumentation import register, using_attributes
from fi_instrumentation.fi_types import ProjectType
from traceai_anthropic import AnthropicInstrumentor
import anthropic

# 1. Set environment variables
os.environ["FI_API_KEY"] = "your-api-key"
os.environ["FI_SECRET_KEY"] = "your-secret-key"
os.environ["ANTHROPIC_API_KEY"] = "your-anthropic-key"

# 2. Register and instrument
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="anthropic_streaming"
)

AnthropicInstrumentor().instrument(tracer_provider=trace_provider)

# 3. Create client
client = anthropic.Anthropic()

def stream_chat(user_message: str, session_id: str = None) -> str:
    """Stream a response from Claude."""

    with using_attributes(
        session_id=session_id,
        metadata={"streaming": True}
    ):
        full_response = ""

        with client.messages.stream(
            model="claude-3-opus-20240229",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_message}]
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                full_response += text

        print()  # Newline
        return full_response

# 4. Use it
if __name__ == "__main__":
    response = stream_chat(
        "Write a haiku about programming.",
        session_id="stream-session-001"
    )
    print(f"\nFull response: {response}")
```

## Trace Attributes

Even with streaming, traceAI captures complete data:

| Attribute | Value |
|-----------|-------|
| `fi.span_kind` | `LLM` |
| `llm.system` | `anthropic` |
| `llm.model_name` | `claude-3-opus-20240229` |
| `llm.input_messages` | User message |
| `llm.output_messages` | Complete streamed response |
| `llm.token_count.prompt` | Input tokens |
| `llm.token_count.completion` | Output tokens |

The span completes when the stream finishes.

## Non-Streaming Comparison

```python
def chat(user_message: str) -> str:
    """Non-streaming chat."""
    response = client.messages.create(
        model="claude-3-opus-20240229",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text
```

## With System Prompt

```python
def stream_with_system(
    system_prompt: str,
    user_message: str
) -> str:
    """Stream with a system prompt."""
    full_response = ""

    with client.messages.stream(
        model="claude-3-opus-20240229",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            full_response += text

    print()
    return full_response

# Use it
response = stream_with_system(
    system_prompt="You are a helpful coding assistant.",
    user_message="Explain Python list comprehensions."
)
```

## With Tool Use

```python
import json

tools = [
    {
        "name": "get_stock_price",
        "description": "Get the current stock price for a symbol",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Stock symbol (e.g., AAPL)"
                }
            },
            "required": ["symbol"]
        }
    }
]

def get_stock_price(symbol: str) -> dict:
    """Mock stock price function."""
    prices = {"AAPL": 175.50, "GOOGL": 140.25, "MSFT": 380.00}
    return {"symbol": symbol, "price": prices.get(symbol, 100.00)}

def chat_with_tools(user_message: str) -> str:
    """Chat with tool support."""

    # Initial request
    response = client.messages.create(
        model="claude-3-opus-20240229",
        max_tokens=1024,
        tools=tools,
        messages=[{"role": "user", "content": user_message}]
    )

    # Check for tool use
    if response.stop_reason == "tool_use":
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                if block.name == "get_stock_price":
                    result = get_stock_price(block.input["symbol"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })

        # Continue with tool results
        final_response = client.messages.create(
            model="claude-3-opus-20240229",
            max_tokens=1024,
            tools=tools,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results}
            ]
        )

        return final_response.content[0].text

    return response.content[0].text

# Use it
print(chat_with_tools("What's the stock price of Apple?"))
```

## Async Streaming

```python
import asyncio
from anthropic import AsyncAnthropic

async def async_stream_chat(user_message: str) -> str:
    """Async streaming chat."""
    client = AsyncAnthropic()
    full_response = ""

    async with client.messages.stream(
        model="claude-3-opus-20240229",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}]
    ) as stream:
        async for text in stream.text_stream:
            print(text, end="", flush=True)
            full_response += text

    print()
    return full_response

# Run
result = asyncio.run(async_stream_chat("Tell me a joke."))
```

## With Events

```python
def stream_with_events(user_message: str) -> str:
    """Stream with event handling."""
    full_response = ""

    with client.messages.stream(
        model="claude-3-opus-20240229",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}]
    ) as stream:
        for event in stream:
            if event.type == "content_block_delta":
                if hasattr(event.delta, "text"):
                    text = event.delta.text
                    print(text, end="", flush=True)
                    full_response += text
            elif event.type == "message_stop":
                print("\n[Stream complete]")

    return full_response
```

## With Experiments

```python
from fi_instrumentation.fi_types import (
    EvalTag, EvalTagType, EvalSpanKind, EvalName, ModelChoices
)

eval_tags = [
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.TOXICITY,
        custom_eval_name="claude_toxicity",
        mapping={"output": "raw.output"},
        model=ModelChoices.PROTECT_FLASH
    ),
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.IS_HELPFUL,
        custom_eval_name="claude_helpfulness",
        mapping={"output": "raw.output"},
        model=ModelChoices.TURING_SMALL
    )
]

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="claude_experiment",
    project_version_name="v1.0",
    eval_tags=eval_tags
)
```

## Multi-Turn Conversation

```python
def conversation():
    """Multi-turn conversation with memory."""
    messages = []

    def chat(user_message: str) -> str:
        messages.append({"role": "user", "content": user_message})

        full_response = ""
        with client.messages.stream(
            model="claude-3-opus-20240229",
            max_tokens=1024,
            messages=messages
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                full_response += text

        print()
        messages.append({"role": "assistant", "content": full_response})
        return full_response

    # Conversation
    with using_attributes(session_id="conversation-001"):
        chat("My name is Alice.")
        chat("What's my name?")  # Should remember
        chat("Tell me a fun fact.")

conversation()
```

## Error Handling

```python
def safe_stream(user_message: str) -> str:
    """Stream with error handling."""
    try:
        with using_attributes(metadata={"safe_mode": True}):
            return stream_chat(user_message)
    except anthropic.APIError as e:
        # API errors are captured in trace
        print(f"API error: {e}")
        return ""
    except Exception as e:
        print(f"Error: {e}")
        return ""
```

## Related

- [Basic OpenAI](basic-openai.md)
- [LangChain RAG](langchain-rag.md)
- [Context Managers](../../python/context-managers.md)
