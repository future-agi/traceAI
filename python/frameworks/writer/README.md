# traceAI-writer

OpenTelemetry instrumentation for the [Writer Python SDK](https://github.com/writer/writer-python)
(`writer-sdk`, import `writerai`).

```bash
pip install traceAI-writer
```

```python
from fi_instrumentation import register
from traceai_writer import WriterInstrumentor
from writerai import Writer

WriterInstrumentor().instrument(tracer_provider=register(project_name="my-project"))

client = Writer()
client.chat.chat(model="palmyra-x5", messages=[{"role": "user", "content": "Hello"}])
```

With `fi-instrumentation-otel` auto-instrumentation the `writer` integration is enabled whenever
`writerai` is installed.

## What is traced

`chat.chat` (`writer.chat`) and `completions.create` (`writer.text_completion`), sync and async,
streaming or not. Each is an `LLM` span with `gen_ai.provider.name = writer`, the request model,
temperature / top_p / max_tokens / stop, the response model and id, finish reasons and token usage
when the response carries it. Tool calls the model makes are recorded on the output message.

* Only the outermost SDK call is traced: `chat.stream()`, `with_raw_response` and
  `with_streaming_response` produce one span, not several.
* Streams end on exhaustion, error or `close()`; an early close is marked
  `writer.stream.incomplete`. This includes leaving a `chat.stream()` helper block early (the
  helper closes the underlying response) and a stream that is dropped without being closed.
* Exceptions are re-raised unchanged and recorded as `ERROR` with `error.type` (the class name);
  the message is never exported.
* Inputs and outputs follow the `TraceConfig` hide settings. Only plain-text conversations are
  captured as `input.value`; multimodal parts, and Knowledge Graph / web-search source documents
  returned with a response, are never exported.

Supported SDK range: `writer-sdk >= 2.0.0, < 4`.
