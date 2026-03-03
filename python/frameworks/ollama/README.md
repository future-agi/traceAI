# TraceAI Ollama Instrumentation

OpenTelemetry instrumentation for [Ollama](https://ollama.ai/) - the leading local LLM runner.

## Installation

```bash
pip install traceai-ollama
```

## Features

- Automatic tracing of Ollama API calls
- Support for chat, generate, and embed endpoints
- Streaming response support
- Token usage tracking
- Performance metrics (total_duration, eval_duration, etc.)
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
from traceai_ollama import OllamaInstrumentor
import ollama

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument Ollama
OllamaInstrumentor().instrument(tracer_provider=provider)

# Use Ollama - calls are automatically traced
response = ollama.chat(
    model="llama3.2",
    messages=[
        {"role": "user", "content": "Hello!"}
    ]
)
print(response["message"]["content"])
```

### Chat Completions

```python
import ollama

# Simple chat
response = ollama.chat(
    model="llama3.2",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"}
    ]
)

# Multi-turn conversation
messages = [
    {"role": "user", "content": "My name is Alice."},
    {"role": "assistant", "content": "Hello Alice! Nice to meet you."},
    {"role": "user", "content": "What's my name?"}
]
response = ollama.chat(model="llama3.2", messages=messages)
```

### Streaming Responses

```python
import ollama

# Streaming chat
stream = ollama.chat(
    model="llama3.2",
    messages=[{"role": "user", "content": "Tell me a story."}],
    stream=True
)

for chunk in stream:
    print(chunk["message"]["content"], end="", flush=True)
```

### Text Generation

```python
import ollama

# Simple text generation
response = ollama.generate(
    model="llama3.2",
    prompt="The quick brown fox"
)
print(response["response"])

# With system prompt
response = ollama.generate(
    model="llama3.2",
    prompt="Write a haiku",
    system="You are a poet."
)
```

### Embeddings

```python
import ollama

# Single embedding
response = ollama.embed(
    model="nomic-embed-text",
    input="Hello, world!"
)
print(f"Embedding dimensions: {len(response['embedding'])}")

# Multiple embeddings (batch)
response = ollama.embeddings(
    model="nomic-embed-text",
    prompt=["Hello", "World"]
)
```

### Using Client Class

```python
import ollama

# Create a client
client = ollama.Client(host="http://localhost:11434")

# Use the client
response = client.chat(
    model="llama3.2",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### Async Support

```python
import asyncio
import ollama

async def main():
    client = ollama.AsyncClient()

    # Async chat
    response = await client.chat(
        model="llama3.2",
        messages=[{"role": "user", "content": "Hello!"}]
    )
    print(response["message"]["content"])

    # Async streaming
    async for chunk in await client.chat(
        model="llama3.2",
        messages=[{"role": "user", "content": "Tell me a joke."}],
        stream=True
    ):
        print(chunk["message"]["content"], end="", flush=True)

asyncio.run(main())
```

### Multimodal (Vision)

```python
import ollama

# With image (base64 encoded)
response = ollama.chat(
    model="llava",
    messages=[
        {
            "role": "user",
            "content": "What's in this image?",
            "images": ["base64_encoded_image_data"]
        }
    ]
)
```

## Configuration Options

### TraceConfig

```python
from fi_instrumentation import TraceConfig
from traceai_ollama import OllamaInstrumentor

config = TraceConfig(
    hide_inputs=False,      # Set True to hide input content
    hide_outputs=False,     # Set True to hide output content
    base64_image_max_length=100,  # Max length for base64 images in traces
)

OllamaInstrumentor().instrument(
    tracer_provider=provider,
    config=config
)
```

## Captured Attributes

### Request Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "LLM" for chat/generate, "EMBEDDING" for embed |
| `llm.system` | "ollama" |
| `llm.provider` | "ollama" |
| `llm.model` | Model name (llama3.2, etc.) |
| `llm.input_messages.{n}.role` | Message role |
| `llm.input_messages.{n}.content` | Message content |

### Response Attributes

| Attribute | Description |
|-----------|-------------|
| `llm.token_count.prompt` | Input token count (prompt_eval_count) |
| `llm.token_count.completion` | Output token count (eval_count) |
| `llm.token_count.total` | Total token count |
| `llm.output_messages.{n}.role` | Response role |
| `llm.output_messages.{n}.content` | Response content |
| `ollama.total_duration_ns` | Total request duration (nanoseconds) |
| `ollama.load_duration_ns` | Model load duration |
| `ollama.prompt_eval_duration_ns` | Prompt evaluation duration |
| `ollama.eval_duration_ns` | Response generation duration |

## Supported Models

Any model available in Ollama can be traced, including:

| Model | Description |
|-------|-------------|
| `llama3.2` | Meta's Llama 3.2 |
| `mistral` | Mistral 7B |
| `mixtral` | Mixtral 8x7B |
| `codellama` | Code Llama |
| `llava` | LLaVA (vision) |
| `nomic-embed-text` | Nomic embeddings |

## Real-World Use Cases

### RAG Pipeline

```python
import ollama

# Generate embedding for query
query = "What is machine learning?"
query_embedding = ollama.embed(
    model="nomic-embed-text",
    input=query
)

# Search vector database (not shown)
# context = search_vector_db(query_embedding["embedding"])

# Generate response with context
response = ollama.chat(
    model="llama3.2",
    messages=[
        {"role": "system", "content": f"Use this context: {context}"},
        {"role": "user", "content": query}
    ]
)
```

### Code Assistant

```python
import ollama

response = ollama.chat(
    model="codellama",
    messages=[
        {"role": "system", "content": "You are a Python expert."},
        {"role": "user", "content": "Write a function to calculate fibonacci numbers"}
    ]
)
```

## License

Apache-2.0
