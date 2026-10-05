# traceAI-voyage

OpenTelemetry instrumentation for the official
[Voyage AI Python client](https://github.com/voyage-ai/voyageai-python)
(`voyageai`).

`traceAI-openai` does not trace Voyage: it wraps the OpenAI client only and
never sees `voyageai.Client`. Use this package for Voyage embeddings and
reranking.

## Installation

```bash
pip install traceAI-voyage
```

It accepts `voyageai>=0.3.7,<1` (0.3.7 is the first release that routes Atlas
keys) and `fi-instrumentation-otel>=1.1.0`. The suite runs on Python 3.10,
3.11, 3.12 and 3.13 against `voyageai` 0.3.7 and 0.5.0.

## Usage

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
```

`examples/embed_and_rerank.py` is a runnable version of this.

The methods are patched on the client classes, so a client built before
`instrument()` is traced too; only calls made while instrumented are traced.
`uninstrument()` restores the original methods.

## Hosts and `base_url`

The instrumentor never sets or changes `base_url`. The Voyage client picks the
host from the API key, and a `base_url` you pass to the client yourself wins.
In `voyageai` 0.3.7 to 0.5.0, keys that start with `al-` (created in MongoDB
Atlas) go to `https://ai.mongodb.com/v1` and other keys (created on the Voyage
platform) to `https://api.voyageai.com/v1`. MongoDB also documents geography
hosts such as `eu.ai.mongodb.com` and `us.ai.mongodb.com`; to use one, pass it
as `base_url` when you construct the client.

The span records only the host the client is using, as `server.address`. A
key sent to the wrong host fails in the client; the span records that error
and nothing retries against another host.

## What is traced

| Call | Span name | `gen_ai.span.kind` |
|---|---|---|
| `Client.embed`, `AsyncClient.embed` | `voyage.embed` | `EMBEDDING` |
| `Client.rerank`, `AsyncClient.rerank` | `voyage.rerank` | `RERANKER` |

One span per call: a list of texts is one span, and the client's own retries
(`max_retries`) happen inside that one span. The span is a child of the active
span when there is one, and it is current while the SDK sends the request, so
a span from an HTTP client instrumentation nests under it. Local models that
`embed` runs in process (`voyageai` 0.5+, for example `voyage-4-nano`) are
traced the same way, without `server.address`; their token count is the one the
client computes locally.

Not traced (no span, not an error): `multimodal_embed`,
`contextualized_embed`, the deprecated module functions
`voyageai.get_embedding(s)` / `aget_embedding(s)`, and the TypeScript client.

## Span attributes

Always recorded when known (never content):

| Attribute | Calls | Value |
|---|---|---|
| `gen_ai.provider.name` | all | `voyage` |
| `gen_ai.operation.name` | all | `embeddings` or `rerank` |
| `gen_ai.request.model` | all | The model; for `embed` without `model`, the SDK default it falls back to. |
| `embedding.model_name` / `reranker.model_name` | embed / rerank | Same model. |
| `server.address` | all | Host from the client's base URL (no path, query or credentials). |
| `voyage.embedding.count` | embed | Number of texts in the call (a bare string is 1). |
| `voyage.input_type`, `voyage.output_dtype`, `voyage.output_dimension` | embed | As passed; absent when not passed. |
| `voyage.embedding.dimension`, `gen_ai.embeddings.dimension.count` | embed, success | Length of the first returned vector; times 8 for the packed `binary` / `ubinary` dtypes. |
| `voyage.rerank.document_count` | rerank | Number of documents sent. |
| `reranker.top_k` | rerank | As passed; absent when not passed. |
| `voyage.rerank.result_count` | rerank, success | Number of results returned. |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.total_tokens` | success | The client's `total_tokens`. Voyage reports one total and every embedding or rerank token is input, so both keys carry it. Never estimated. No cost is set. |
| `voyage.cancelled` | cancelled calls | `true`. |

Usage is written only on the Voyage span, once, on the `gen_ai.usage.*` keys
the Future AGI collector reads. If another instrumentation in your app (for
example a framework that wraps the same embedding call) also records usage for
it, the trace counts those tokens twice.

## Privacy and content capture

By default the rerank span records the query and the relevance scores, as PRD
J2 / AC-03 require, so this default is not a deviation from the spec. Embed
texts and rerank documents are recorded only with `capture_content=True`.
Embedding vectors are never recorded.

| Attribute | Calls | Value | Recorded | Dropped by |
|---|---|---|---|---|
| `reranker.query` | rerank | The query. | By default | `hide_inputs`, `hide_input_text` |
| `input.value` (`input.mime_type` = `text/plain`) | rerank | The query. | By default | `hide_inputs`, `hide_input_text` |
| `output.value` (`output.mime_type` = `application/json`) | rerank | JSON list of `{"index", "relevance_score"}` in the client's order. | By default | `hide_outputs` |
| `input.value` (`application/json`) | rerank | JSON `{"query": ..., "documents": [...]}`, in place of the plain query. | Only with `capture_content=True` | `hide_inputs`, `hide_input_text` |
| `input.value` (`application/json`) | embed | JSON list of the texts. | Only with `capture_content=True` | `hide_inputs`, `hide_input_text` |

Hiding inputs keeps the scores (they are numbers, not input text), and hiding
outputs keeps the query. The counts (`voyage.embedding.count`,
`voyage.rerank.document_count`, `voyage.rerank.result_count`) stay whatever is
hidden. `TraceConfig` reads `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` from the
environment, so `FI_HIDE_INPUTS=true FI_HIDE_OUTPUTS=true` records no content
at all.

To record embed texts and rerank documents too, opt in:

```python
from fi_instrumentation import TraceConfig

VoyageInstrumentor().instrument(
    tracer_provider=trace_provider,
    capture_content=True,
    config=TraceConfig(),  # optional; honours hide_inputs / hide_outputs / pii_redaction
)
```

At most 64 texts, 64 documents and 64 scores (the first 64 results, in the
client's order) are recorded, and the query and each text or document are
cut to 2 KB of UTF-8 on a character boundary; `voyage.embedding.count`,
`voyage.rerank.document_count` and `voyage.rerank.result_count` stay exact.

`TraceConfig(pii_redaction=True)` (or `FI_PII_REDACTION=true`) applies
`fi_instrumentation`'s regex PII redaction, which replaces email addresses,
SSNs, card numbers, `sk-`/`pk-` live/test/prod API keys, IPv4 addresses and
phone numbers with tokens such as `<EMAIL_ADDRESS>`. It covers:

- every string span attribute: the query, captured texts and documents, the
  scores JSON, and the other attributes above (an IP `server.address` becomes
  `<IP_ADDRESS>`);
- the error status description and the `exception` event's
  `exception.message` and `exception.stacktrace`, after the API key is
  removed from them.

It does not change span names, event names or `exception.type`. The patterns
match digits, not meaning: a relevance score with 10 or more digits after the
decimal point is rewritten too (for example `0.<CREDIT_CARD>`), which leaves
that `output.value` as invalid JSON.

Embedding vectors are never recorded, with or without capture, and
`hide_embedding_vectors` has nothing to hide here.

The Voyage API key is never recorded. The instrumentor does not read it from
the environment itself; it takes the key the client holds (`client.api_key`,
`client._params["api_key"]`) and a module-level `voyageai.api_key`, and
replaces any occurrence in the recorded query, captured content, error
messages and stack traces with `[redacted]`.

## Errors and cancellation

An error raised by the client (for example `voyageai.error.AuthenticationError`
for HTTP 401, or `voyageai.error.Timeout`) sets the span status to ERROR with
`<ExceptionType>: <message>` and records one `exception` event; the key is
redacted from both, and with `pii_redaction=True` so is PII (see above). The
exception reaches your code unchanged.

Cancelling an `AsyncClient` call (`asyncio.CancelledError`) or interrupting a
blocking call (`KeyboardInterrupt`) ends the span with status ERROR,
description `cancelled` and `voyage.cancelled` = `true`, with no exception
event. Result attributes and usage are absent on error and cancellation, never
written as 0.

The instrumentation never changes what a call returns or raises: a failure
while building attributes or starting the span is logged at debug level and the
Voyage call runs as if uninstrumented.

## Configuration errors

`instrument(config=...)` must be a `fi_instrumentation.TraceConfig` and
`capture_content` a `bool`; anything else raises `TypeError` and wraps
nothing. On an installed `voyageai` outside `>=0.3.7,<1`, `instrument()` logs
an error and wraps nothing (OpenTelemetry's dependency check).

## License

Apache-2.0.
