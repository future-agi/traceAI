# TraceAI HuggingFace Instrumentation

OpenTelemetry instrumentation for [HuggingFace Inference API](https://huggingface.co/docs/huggingface_hub/guides/inference) - text generation, chat completion, and embeddings.

## Installation

```bash
pip install traceai-huggingface
```

## Features

- Automatic tracing of HuggingFace Inference API calls
- Support for text generation, chat completion, and feature extraction (embeddings)
- Streaming response support
- Token usage tracking
- Both sync and async client support
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```python
from huggingface_hub import InferenceClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_huggingface import HuggingFaceInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument HuggingFace
HuggingFaceInstrumentor().instrument(tracer_provider=provider)

# Use HuggingFace
client = InferenceClient(token="your-hf-token")
response = client.text_generation("Hello, how are you?", model="meta-llama/Llama-2-7b-chat-hf")
print(response)
```

### Text Generation

```python
from huggingface_hub import InferenceClient

client = InferenceClient()

# Simple text generation
response = client.text_generation(
    "The capital of France is",
    model="meta-llama/Llama-2-7b-chat-hf",
    max_new_tokens=100,
    temperature=0.7,
)
print(response)

# With more parameters
response = client.text_generation(
    "Write a poem about Python programming",
    model="meta-llama/Llama-2-7b-chat-hf",
    max_new_tokens=200,
    temperature=0.9,
    top_p=0.95,
    repetition_penalty=1.1,
    do_sample=True,
)
print(response)
```

### Chat Completion

```python
from huggingface_hub import InferenceClient

client = InferenceClient()

# Chat completion with messages
messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is machine learning?"}
]

response = client.chat_completion(
    messages=messages,
    model="meta-llama/Llama-2-7b-chat-hf",
    max_tokens=500,
    temperature=0.7,
)
print(response.choices[0].message.content)

# Multi-turn conversation
messages = [
    {"role": "user", "content": "My name is Alice"},
    {"role": "assistant", "content": "Hello Alice! Nice to meet you."},
    {"role": "user", "content": "What's my name?"}
]

response = client.chat_completion(
    messages=messages,
    model="meta-llama/Llama-2-7b-chat-hf",
)
print(response.choices[0].message.content)
```

### Streaming

```python
from huggingface_hub import InferenceClient

client = InferenceClient()

# Streaming text generation
for chunk in client.text_generation(
    "Tell me a story about a brave knight",
    model="meta-llama/Llama-2-7b-chat-hf",
    max_new_tokens=500,
    stream=True,
):
    print(chunk, end="", flush=True)
print()

# Streaming chat completion
messages = [{"role": "user", "content": "Count to 10"}]
for chunk in client.chat_completion(
    messages=messages,
    model="meta-llama/Llama-2-7b-chat-hf",
    stream=True,
):
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### Feature Extraction (Embeddings)

```python
from huggingface_hub import InferenceClient

client = InferenceClient()

# Single text embedding
embedding = client.feature_extraction(
    "Hello world",
    model="sentence-transformers/all-MiniLM-L6-v2",
)
print(f"Embedding dimensions: {len(embedding[0])}")

# Multiple texts
texts = ["Hello world", "Machine learning is great", "Python is awesome"]
embeddings = [
    client.feature_extraction(text, model="sentence-transformers/all-MiniLM-L6-v2")
    for text in texts
]
print(f"Generated {len(embeddings)} embeddings")
```

### Async Client

```python
import asyncio
from huggingface_hub import AsyncInferenceClient

async def main():
    client = AsyncInferenceClient()

    # Async text generation
    response = await client.text_generation(
        "The future of AI is",
        model="meta-llama/Llama-2-7b-chat-hf",
        max_new_tokens=100,
    )
    print(response)

    # Async chat completion
    messages = [{"role": "user", "content": "Hello!"}]
    response = await client.chat_completion(
        messages=messages,
        model="meta-llama/Llama-2-7b-chat-hf",
    )
    print(response.choices[0].message.content)

    # Async embeddings
    embedding = await client.feature_extraction(
        "Hello world",
        model="sentence-transformers/all-MiniLM-L6-v2",
    )
    print(f"Embedding dimensions: {len(embedding[0])}")

asyncio.run(main())
```

### Tool Use

```python
from huggingface_hub import InferenceClient

client = InferenceClient()

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city name"
                    }
                },
                "required": ["location"]
            }
        }
    }
]

messages = [{"role": "user", "content": "What's the weather in Paris?"}]

response = client.chat_completion(
    messages=messages,
    model="meta-llama/Llama-2-7b-chat-hf",
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
from traceai_huggingface import HuggingFaceInstrumentor

config = TraceConfig(
    hide_inputs=False,
    hide_outputs=False,
)

HuggingFaceInstrumentor().instrument(
    tracer_provider=provider,
    config=config
)
```

## Captured Attributes

### Text Generation Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "LLM" |
| `llm.system` | "huggingface" |
| `llm.provider` | "huggingface" |
| `llm.model` | Model name |
| `llm.token_count.completion` | Output token count |
| `huggingface.finish_reason` | Response finish reason |

### Chat Completion Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "LLM" |
| `llm.system` | "huggingface" |
| `llm.model` | Model name |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |
| `huggingface.tools_count` | Number of tools provided |
| `huggingface.finish_reason` | Response finish reason |

### Feature Extraction Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "EMBEDDING" |
| `embedding.model` | Embedding model name |
| `huggingface.texts_count` | Number of texts embedded |
| `huggingface.embedding_dimensions` | Vector dimensions |

## Supported Models

HuggingFace Inference API supports thousands of models. Here are some popular ones:

| Category | Example Models |
|----------|----------------|
| Text Generation | `meta-llama/Llama-2-7b-chat-hf`, `mistralai/Mistral-7B-Instruct-v0.1` |
| Chat | `meta-llama/Llama-2-70b-chat-hf`, `HuggingFaceH4/zephyr-7b-beta` |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2`, `BAAI/bge-large-en-v1.5` |

## Real-World Use Cases

### Semantic Search with Embeddings

```python
from huggingface_hub import InferenceClient
import numpy as np

client = InferenceClient()

# Index documents
documents = [
    "Python is a programming language",
    "Machine learning uses algorithms",
    "Paris is the capital of France",
]

doc_embeddings = [
    client.feature_extraction(doc, model="sentence-transformers/all-MiniLM-L6-v2")
    for doc in documents
]

# Search
query = "What programming languages are there?"
query_embedding = client.feature_extraction(
    query, model="sentence-transformers/all-MiniLM-L6-v2"
)

# Compute cosine similarity
def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

similarities = [
    cosine_similarity(query_embedding[0], doc_emb[0])
    for doc_emb in doc_embeddings
]

# Get most similar
best_idx = np.argmax(similarities)
print(f"Most similar document: {documents[best_idx]}")
```

### RAG Pipeline

```python
from huggingface_hub import InferenceClient

client = InferenceClient()

# Retrieve relevant documents (simplified)
documents = [
    "The Eiffel Tower is 330 meters tall.",
    "Paris has a population of 2.1 million.",
]

# Generate answer with context
context = "\n".join(documents)
messages = [
    {"role": "system", "content": f"Answer based on this context:\n{context}"},
    {"role": "user", "content": "How tall is the Eiffel Tower?"}
]

response = client.chat_completion(
    messages=messages,
    model="meta-llama/Llama-2-7b-chat-hf",
    max_tokens=200,
)

print(response.choices[0].message.content)
```

### Batch Processing

```python
import asyncio
from huggingface_hub import AsyncInferenceClient

async def process_batch(texts, model):
    client = AsyncInferenceClient()

    tasks = [
        client.text_generation(text, model=model, max_new_tokens=100)
        for text in texts
    ]

    return await asyncio.gather(*tasks)

texts = [
    "Summarize: AI is transforming industries...",
    "Summarize: Climate change affects...",
    "Summarize: The economy is growing...",
]

results = asyncio.run(process_batch(texts, "meta-llama/Llama-2-7b-chat-hf"))
for text, result in zip(texts, results):
    print(f"Input: {text[:50]}...")
    print(f"Output: {result}\n")
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `HF_TOKEN` | HuggingFace API token |
| `HUGGINGFACE_HUB_TOKEN` | Alternative token variable |

## License

Apache-2.0
