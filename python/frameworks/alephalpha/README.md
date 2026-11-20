# traceAI-alephalpha

OpenTelemetry instrumentation for the [Aleph Alpha Python SDK](https://github.com/Aleph-Alpha/aleph-alpha-client)
(`aleph-alpha-client`, import `aleph_alpha_client`).

```bash
pip install traceAI-alephalpha
```

```python
from aleph_alpha_client import Client, CompletionRequest, Prompt
from fi_instrumentation import register
from traceai_alephalpha import AlephAlphaInstrumentor

AlephAlphaInstrumentor().instrument(tracer_provider=register(project_name="my-project"))

client = Client(token="...", host="https://inference-api.your.domain")
client.complete(CompletionRequest(prompt=Prompt.from_text("Hello"), maximum_tokens=16), model="luminous-base")
```

With `fi-instrumentation-otel` auto-instrumentation the `alephalpha` integration is enabled whenever
`aleph_alpha_client` is installed.

## What is traced

On `Client` and `AsyncClient` (sync and async):

| SDK call | Span | Kind |
| --- | --- | --- |
| `complete` | `alephalpha.text_completion` | `LLM` |
| `chat` | `alephalpha.chat` | `LLM` |
| `evaluate` | `alephalpha.evaluate` | `LLM` |
| `embed`, `embeddings`, `semantic_embed`, `batch_semantic_embed`, `instructable_embed` | `alephalpha.embeddings` | `EMBEDDING` |
| `AsyncClient.complete_with_streaming`, `AsyncClient.chat_with_streaming` | `alephalpha.text_completion`, `alephalpha.chat` | `LLM` |

Each span carries `gen_ai.provider.name = aleph_alpha`, the request model, max tokens / temperature /
top_p / top_k / penalties / stop sequences, the response model, finish reasons and token usage when
the response carries it. Tool calls the model makes are recorded on the output message. Embedding
vectors are never recorded. Token usage comes from what the SDK returns: a non-streaming `chat` response
has none (the SDK drops it), a stream reports it when `stream_options.include_usage` is set.

* Only the outermost SDK call is traced; calls an SDK method makes into other traced methods do not
  produce extra spans.
* Streams end on exhaustion, error or `aclose()`; an early exit is marked `alephalpha.stream.incomplete`.
  A stream that is dropped without being read or closed also ends its span.
* Exceptions are re-raised unchanged and recorded as `ERROR` with `error.type` (the class name);
  the message is never exported.
* Inputs and outputs follow the `TraceConfig` hide settings. Only text prompts and plain-text chat
  messages are captured as `input.value`; images and token-id prompts are never exported.
* A failure inside the instrumentation never breaks the SDK call.

Supported SDK range: `aleph-alpha-client >= 7.6.0, < 12`. Methods an SDK version does not have
(for example `embeddings` in older releases) are skipped.
