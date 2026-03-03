# TraceAI Together Instrumentation

OpenTelemetry instrumentation for [Together AI](https://together.ai/) - chat completions, completions, and embeddings APIs.

## Installation

```bash
pip install traceai-together
```

## Features

- Automatic tracing of Together AI API calls
- Support for chat completions, completions, and embeddings endpoints
- Streaming response support for both sync and async clients
- Token usage tracking
- Tool/function calling support
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```python
import together
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_together import TogetherInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument Together AI
TogetherInstrumentor().instrument(tracer_provider=provider)

# Use Together AI
client = together.Together(api_key="your-api-key")
response = client.chat.completions.create(
    model="meta-llama/Llama-3-8b-chat-hf",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### Chat Completions

```python
import together

client = together.Together()

# Simple chat
response = client.chat.completions.create(
    model="meta-llama/Llama-3-8b-chat-hf",
    messages=[{"role": "user", "content": "What is machine learning?"}],
    max_tokens=512,
)
print(response.choices[0].message.content)

# With system message
response = client.chat.completions.create(
    model="meta-llama/Llama-3-8b-chat-hf",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain quantum computing."},
    ],
    temperature=0.7,
)
```

### Streaming Chat

```python
import together

client = together.Together()

# Streaming response
stream = client.chat.completions.create(
    model="meta-llama/Llama-3-8b-chat-hf",
    messages=[{"role": "user", "content": "Tell me a story"}],
    stream=True,
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### Completions (Legacy)

```python
import together

client = together.Together()

# Text completion
response = client.completions.create(
    model="meta-llama/Llama-3-8b-hf",
    prompt="The quick brown fox",
    max_tokens=50,
)
print(response.choices[0].text)
```

### Embeddings

```python
import together

client = together.Together()

# Generate embeddings
response = client.embeddings.create(
    model="togethercomputer/m2-bert-80M-8k-retrieval",
    input=["Hello world", "Machine learning is great"],
)
print(f"Generated {len(response.data)} embeddings")
print(f"Dimensions: {len(response.data[0].embedding)}")
```

### Async Client

```python
import asyncio
import together

async def main():
    client = together.AsyncTogether()

    # Async chat completion
    response = await client.chat.completions.create(
        model="meta-llama/Llama-3-8b-chat-hf",
        messages=[{"role": "user", "content": "Hello!"}],
    )
    print(response.choices[0].message.content)

    # Async streaming
    stream = await client.chat.completions.create(
        model="meta-llama/Llama-3-8b-chat-hf",
        messages=[{"role": "user", "content": "Tell me a joke"}],
        stream=True,
    )

    async for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)

asyncio.run(main())
```

### Tool/Function Calling

```python
import together
import json

client = together.Together()

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a location",
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
]

response = client.chat.completions.create(
    model="meta-llama/Llama-3-8b-chat-hf",
    messages=[{"role": "user", "content": "What's the weather in Paris?"}],
    tools=tools,
    tool_choice="auto",
)

if response.choices[0].message.tool_calls:
    for tool_call in response.choices[0].message.tool_calls:
        print(f"Tool: {tool_call.function.name}")
        print(f"Arguments: {tool_call.function.arguments}")
```

## Configuration Options

### TraceConfig

```python
from fi_instrumentation import TraceConfig
from traceai_together import TogetherInstrumentor

config = TraceConfig(
    hide_inputs=False,
    hide_outputs=False,
)

TogetherInstrumentor().instrument(
    tracer_provider=provider,
    config=config
)
```

## Captured Attributes

### Chat Completions Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "LLM" |
| `llm.system` | "together" |
| `llm.provider` | "together" |
| `llm.model` | Model name (e.g., meta-llama/Llama-3-8b-chat-hf) |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |
| `llm.input_messages` | Input messages array |
| `llm.output_messages` | Output messages array |
| `llm.invocation_parameters` | Model parameters (temperature, max_tokens, etc.) |

### Completions Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "LLM" |
| `llm.system` | "together" |
| `llm.model` | Model name |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `input.value` | Input prompt |
| `output.value` | Generated text |

### Embeddings Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "EMBEDDING" |
| `llm.system` | "together" |
| `embedding.model` | Embedding model name |
| `together.texts_count` | Number of texts embedded |
| `together.embeddings_count` | Number of embeddings returned |
| `together.embedding_dimensions` | Vector dimensions |

## Available Models

Together AI provides access to many open-source models. Some popular ones include:

| Category | Models |
|----------|--------|
| Chat | `meta-llama/Llama-3-8b-chat-hf`, `meta-llama/Llama-3-70b-chat-hf`, `mistralai/Mixtral-8x7B-Instruct-v0.1` |
| Completions | `meta-llama/Llama-3-8b-hf`, `meta-llama/Llama-3-70b-hf` |
| Embeddings | `togethercomputer/m2-bert-80M-8k-retrieval`, `BAAI/bge-large-en-v1.5` |

## Real-World Use Cases

### RAG Pipeline

```python
import together

client = together.Together()

# Step 1: Generate embeddings for documents
docs = ["Document 1 content", "Document 2 content", "Document 3 content"]
doc_embeddings = client.embeddings.create(
    model="togethercomputer/m2-bert-80M-8k-retrieval",
    input=docs,
)

# Step 2: Generate embedding for query
query = "What is the main topic?"
query_embedding = client.embeddings.create(
    model="togethercomputer/m2-bert-80M-8k-retrieval",
    input=[query],
)

# Step 3: Find relevant docs (using cosine similarity - not shown)
relevant_docs = docs[:2]  # Simplified

# Step 4: Generate response with context
response = client.chat.completions.create(
    model="meta-llama/Llama-3-8b-chat-hf",
    messages=[
        {"role": "system", "content": f"Context: {' '.join(relevant_docs)}"},
        {"role": "user", "content": query},
    ],
)
print(response.choices[0].message.content)
```

### Multi-turn Conversation

```python
import together

client = together.Together()

messages = []

def chat(user_message):
    messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model="meta-llama/Llama-3-8b-chat-hf",
        messages=messages,
    )

    assistant_message = response.choices[0].message.content
    messages.append({"role": "assistant", "content": assistant_message})

    return assistant_message

# Have a conversation
print(chat("My name is Alice"))
print(chat("What's my name?"))
```

## License

Apache-2.0
