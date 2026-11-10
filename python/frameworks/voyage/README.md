# traceAI-voyage

Trace embeddings and reranking from the official
[Voyage AI Python client](https://github.com/voyage-ai/voyageai-python) in Future AGI.
The same instrumentor supports `voyageai.Client` and `voyageai.AsyncClient`.

## Installation

```bash
pip install traceAI-voyage
```

Requires Python 3.10–3.13, `voyageai>=0.3.7,<1` and
`fi-instrumentation-otel>=1.1.0`.

## Usage

Set `FI_API_KEY`, `FI_SECRET_KEY` and `VOYAGE_API_KEY` in your environment, then
register a trace provider and instrument the client:

```python
import voyageai
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_voyage import VoyageInstrumentor

trace_provider = register(project_type=ProjectType.OBSERVE, project_name="my-rag-app")
VoyageInstrumentor().instrument(tracer_provider=trace_provider)

client = voyageai.Client()  # reads VOYAGE_API_KEY
client.embed(["What is the capital of France?"], model="voyage-3.5", input_type="query")
client.rerank("capital of France", ["Paris is...", "Berlin is..."], model="rerank-2.5", top_k=1)
client.contextualized_embed(
    [["Paris is the capital of France.", "The Seine flows through Paris."]],
    model="voyage-context-3",
    input_type="document",
)
trace_provider.force_flush()
```

Use `client.multimodal_embed(inputs, model="voyage-multimodal-3")` for mixed
text and image inputs. Pass inputs in the format accepted by your Voyage SDK;
the instrumentor does not transform them or invoke chunking callbacks.

Methods are wrapped on the client classes, so a client created before
`instrument()` is traced too. `uninstrument()` restores the original methods.
`examples/embed_and_rerank.py` provides a runnable embedding and reranking example.

## What is traced

The following methods are supported on both clients:

| Method | Span name | `gen_ai.span.kind` |
|---|---|---|
| `embed` | `voyage.embed` | `EMBEDDING` |
| `multimodal_embed` | `voyage.multimodal_embed` | `EMBEDDING` |
| `contextualized_embed` | `voyage.contextualized_embed` | `EMBEDDING` |
| `rerank` | `voyage.rerank` | `RERANKER` |

Each public call produces one span, including an entire nested batch and the
client's own retries. The span is a child of the active span and remains current
while the SDK sends its request, so HTTP instrumentation can nest underneath it.
Local models run by `embed` are traced without `server.address`.

The deprecated module functions `voyageai.get_embedding(s)` / `aget_embedding(s)`
and the TypeScript client are not covered. `traceAI-openai` does not instrument
`voyageai.Client`; use this package rather than an OpenAI wrapper for these calls.

## Span attributes

| Attribute | Value |
|---|---|
| `gen_ai.provider.name` | `voyage` |
| `gen_ai.operation.name` | `embeddings` or `rerank` |
| `gen_ai.request.model` | Requested model, or the SDK default for `embed` without a model |
| `embedding.model_name` / `reranker.model_name` | Model for the operation |
| `server.address` | Host chosen by the client, without URL path, query or credentials |
| `voyage.embedding.count` | Input count for `embed`/`multimodal_embed`; total returned chunk vectors across all documents for successful `contextualized_embed` |
| `voyage.contextualized.document_count` | Number of contextualized input documents, before chunking |
| `voyage.input_type`, `voyage.output_dtype`, `voyage.output_dimension` | Embedding options when supplied |
| `voyage.embedding.dimension`, `gen_ai.embeddings.dimension.count` | First returned vector's dimension; packed `binary` / `ubinary` lengths are multiplied by 8 |
| `voyage.rerank.document_count`, `voyage.rerank.result_count` | Rerank input document and successful result counts |
| `reranker.top_k` | Requested result limit, when supplied |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.total_tokens` | The SDK's reported `total_tokens`, once on the Voyage span; no estimate or cost |
| `voyage.usage.text_tokens`, `voyage.usage.image_pixels`, `voyage.usage.video_pixels` | Multimodal usage as reported by the SDK; pixels are not tokens and are not added to the token total |
| `voyage.cancelled` | `true` on a cancelled call |

Contextualized counts come from the returned nested result, so client-side or
server-side chunking does not confuse document count with embedding count.
Vectors and returned chunk text are never exported.

Disable any other instrumentor wrapping the same Voyage methods to avoid duplicate
spans and usage. Framework-level parent spans may report their own token totals.

## Privacy

| Setting | Captured content |
|---|---|
| Default | Rerank query and relevance scores; no embedding inputs or rerank documents |
| `capture_content=True` | Also embedding text inputs and rerank documents |
| `TraceConfig(hide_inputs=True)` or `FI_HIDE_INPUTS=true` | No input content; rerank scores remain |
| `TraceConfig(hide_input_text=True)` | No input content |
| `TraceConfig(hide_outputs=True)` or `FI_HIDE_OUTPUTS=true` | No rerank scores; input capture is unchanged |
| `TraceConfig(pii_redaction=True)` or `FI_PII_REDACTION=true` | Applies core PII redaction to recorded strings |

```python
from fi_instrumentation import TraceConfig

# Use these options in your instrument() call instead of the default call above.
VoyageInstrumentor().instrument(
    tracer_provider=trace_provider,
    capture_content=True,
    config=TraceConfig(pii_redaction=True),
)
```

Embedding vectors, multimodal media payloads, media URLs and media object
representations are never captured. With capture enabled, multimodal inputs
retain their batch boundaries and text segments; media segments become
`[media omitted]`. Contextualized inputs retain their document/chunk boundaries.
Text supplied explicitly as text is treated as text, even if it contains a URL.

Nested capture is limited to 64 outer groups and 64 segments in total, with each
text capped at 2 KB of UTF-8. Ordinary embedding texts, rerank documents and scores
are limited to 64 items each. Counts and usage remain exact.

Known client API keys are redacted from captured content and exported error
messages. PII redaction is pattern-based: review your capture settings for the
sensitivity of your data. The core redactor can also match long numeric scores;
use `hide_outputs=True` if you do not need scores in traces.

## Errors and cancellation

Failed calls produce an ERROR span and an exception event, without invented
usage or result dimensions. For `multimodal_embed` and `contextualized_embed`,
only the exception type and a generic message are recorded: SDK validation or
service errors can echo media or entire documents. Original errors still reach
your application unchanged. Other methods redact the client key from the error
message and apply the configured core error-privacy policy. Stack traces are
never exported by this instrumentor.

Cancelling an async call or interrupting a blocking call ends its span with
`voyage.cancelled=true` and no exception event. Instrumentation failures do not
change the SDK's return value or raised exception.

## Hosts and configuration

The instrumentor never changes `base_url`. The SDK selects its host from your
key; an explicit `base_url` on the client takes precedence. Set any required
regional host on the client, not the instrumentor. A failed request does not
cause the instrumentor to retry against another host.

`instrument(config=...)` requires a `TraceConfig`; `capture_content` must be a
boolean. Invalid option types raise `TypeError` before wrapping any method.
OpenTelemetry's dependency check prevents instrumentation outside the supported
SDK version range.

## License

Apache-2.0.
