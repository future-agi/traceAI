# Cohere tracing

Trace Cohere chat, streaming chat, embeddings, and reranking with OpenTelemetry.

## Install

```bash
pip install traceai-cohere
```

The instrumentor supports `cohere>=5.0.0`. It instruments `Client` and
`AsyncClient`, plus `ClientV2` and `AsyncClientV2` when those classes are available
in your installed Cohere SDK. Use one Cohere instrumentor per application; do not
also enable another library's Cohere instrumentation.

## Configure tracing

Pass your application's OpenTelemetry tracer provider to the instrumentor. This
example prints spans locally; use your configured OTLP provider to export traces
to [Future AGI](https://docs.futureagi.com/docs/tracing).

Set `CO_API_KEY` to your Cohere API key and `COHERE_MODEL` to a chat model available
to your account before running the example. Keep both outside your source code.

```python
import os

import cohere
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from traceai_cohere import CohereInstrumentor

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
CohereInstrumentor().instrument(tracer_provider=provider)

client = cohere.ClientV2(api_key=os.environ["CO_API_KEY"])
response = client.chat(
    model=os.environ["COHERE_MODEL"],
    messages=[{"role": "user", "content": "Explain semantic search in one sentence."}],
)
for content in response.message.content or []:
    if content.type == "text":
        print(content.text)
provider.force_flush()
```

Existing v1 calls such as `client.chat(message="Hello", model=...)` continue to
work with the same `CohereInstrumentor().instrument(...)` setup.

## Stream responses

The stream span finishes when you exhaust or close the iterator. Close a stream
explicitly if you stop reading early.

```python
stream = client.chat_stream(
    model=os.environ["COHERE_MODEL"],
    messages=[{"role": "user", "content": "Give three tips for semantic search."}],
)
try:
    for event in stream:
        if event.type == "content-delta" and event.delta.message.content.text:
            print(event.delta.message.content.text, end="", flush=True)
finally:
    stream.close()
```

For async applications, use `AsyncClientV2` and iterate directly over
`chat_stream(...)`; do not await the stream itself.

```python
import asyncio

async def main():
    client = cohere.AsyncClientV2(api_key=os.environ["CO_API_KEY"])
    stream = client.chat_stream(
        model=os.environ["COHERE_MODEL"],
        messages=[{"role": "user", "content": "Explain reranking in one sentence."}],
    )
    try:
        async for event in stream:
            if event.type == "content-delta" and event.delta.message.content.text:
                print(event.delta.message.content.text, end="", flush=True)
    finally:
        await stream.aclose()

asyncio.run(main())
provider.force_flush()
```

## Embed and rerank

Both operations use the same instrumentor setup, for sync and async clients.

```python
embedding = client.embed(
    model="embed-v4.0",
    texts=["Paris is the capital of France."],
    input_type="search_document",
    embedding_types=["float"],
)
print(len(embedding.embeddings.float[0]))

ranking = client.rerank(
    model="rerank-v3.5",
    query="What is the capital of France?",
    documents=["Paris is the capital of France.", "London is the capital of England."],
    top_n=1,
)
print(ranking.results[0].index)
```

## Control content capture

By default, chat spans include prompts, responses, and tool-call arguments.
Configure privacy before instrumenting if you do not want that content exported.
Replace the earlier `instrument(...)` call with this one:

```python
from fi_instrumentation import TraceConfig

CohereInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)
```

You can also set `FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true` in your environment.
Explicit `TraceConfig` settings take precedence. Hiding content retains model,
usage, counts, and rerank scores.

## Span attributes

| Operation | Span name | Captured metadata |
| --- | --- | --- |
| Chat | `cohere.chat` | Model, input/output messages, tool calls, token usage, finish reason |
| Streaming chat | `cohere.chat_stream` | Accumulated text and tool-call arguments, final usage and finish reason |
| Embedding | `cohere.embed` | Model, text count, embedding count and dimensions |
| Rerank | `cohere.rerank` | Model, query, result count, relevance scores |

Token usage prefers actual token counts when the response provides them, falling
back to billed units otherwise. A stream closed early may not have final usage.
Tool calls use canonical `gen_ai.input.messages` and `gen_ai.output.messages`
attributes so content privacy settings also apply to their arguments.

To remove the patches, call `CohereInstrumentor().uninstrument()`.

## License

Apache-2.0
