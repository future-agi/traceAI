# traceAI Transformers

Trace local Hugging Face Transformers pipeline inference in Future AGI. For remote Hugging Face InferenceClient calls, use `traceAI-huggingface` instead; the two instrumentors can run together.

## Install

```bash
pip install traceAI-transformers torch
```

Python 3.10–3.13 and Transformers 4.42–5.x are supported. Transformers 5 requires Python 3.11 or later. Use PyTorch pipelines; select the appropriate PyTorch build for your CPU or GPU.

## Trace inference

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment, then configure tracing before calling your pipeline:

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_transformers import TransformersInstrumentor
from transformers import pipeline

provider = register(
    project_name="local-transformers",
    project_type=ProjectType.OBSERVE,
    headers={
        "FI_API_KEY": os.environ["FI_API_KEY"],
        "FI_SECRET_KEY": os.environ["FI_SECRET_KEY"],
    },
    verbose=False,
)
TransformersInstrumentor().instrument(tracer_provider=provider)

generate = pipeline(
    "text-generation",
    model="HuggingFaceTB/SmolLM2-135M-Instruct",
    device=-1,
)
result = generate(
    [{"role": "user", "content": "Explain what a tokenizer does in one sentence."}],
    max_new_tokens=32,
    do_sample=False,
)
print(result[0]["generated_text"][-1]["content"])
provider.force_flush()
provider.shutdown()
```

The first run downloads the model. `register` exports OTLP traces to `https://api.futureagi.com/tracer/v1/traces` with `FI_API_KEY` and `FI_SECRET_KEY` headers. This is a traces-only endpoint, not a destination for OTLP metrics or logs. Open the `local-transformers` project in Future AGI to inspect `transformers.text-generation` spans.

## Content and privacy

By default, only task, model architecture, timing, status and error information are recorded. Model paths, generation parameters, embedding vectors and token usage estimates are not collected.

To include bounded input/output previews:

```python
from fi_instrumentation import TraceConfig

TransformersInstrumentor().instrument(
    tracer_provider=provider,
    capture_content=True,
    config=TraceConfig(hide_error_details=True),
)
```

Choose this configuration on your first `instrument()` call, or call `uninstrument()` before changing it. Previews are limited to 4,096 characters and may be truncated. `TraceConfig` input/output privacy flags override content capture. Hiding inputs also suppresses output previews, because pipeline responses can contain the original prompt or conversation. Set `FI_HIDE_ERROR_DETAILS=true` to omit exception messages, stack traces and error descriptions. Error details can contain inputs even when content capture is off.

## Supported calls

| Pipeline | Span kind | Versions |
| --- | --- | --- |
| Text generation, including chat, batches and streamers | LLM | 4.42–5.x |
| Feature extraction, including tensor results | EMBEDDING | 4.42–5.x |
| Text-to-text generation, summarization and translation | LLM | 4.x only |
| Extractive question answering | CHAIN | 4.x only |

Transformers 5 removed the historical text-to-text, summarization, translation and extractive question-answering pipelines. Use a text-generation chat model on 5.x. Direct `model.generate()` calls, other tasks, TensorFlow pipelines, and model loading are not instrumented.

Generator and dataset calls remain lazy. Each traversal gets a span that starts when the first result is requested and ends on exhaustion, error or explicit `iterator.close()`. Dataset results preserve their length and `DataLoader` compatibility. Starting another traversal preserves the dataset's reset behavior and ends any unfinished span from the previous traversal. The span is current only while producing the next result, not while your application handles it. Close partially consumed iterators to end their spans promptly. Lazy payloads are not collected. Streamer spans end when pipeline inference returns, independently of when queued tokens are consumed. When starting a worker thread, propagate OpenTelemetry context explicitly if it should inherit a parent span.

Instrumentation is idempotent and supports `TransformersInstrumentor().uninstrument()` and re-instrumentation. It uses your supplied tracer provider without replacing the global provider.
