# Trace Haystack with Future AGI

Capture synchronous and asynchronous pipeline runs, component inputs and outputs, model usage, retrieval, and native agent/tool operations in Future AGI.

## Install

Use Python 3.10–3.12. This adapter supports Haystack 2.10 through 3.x; the example uses Haystack 3.4.1.

```bash
pip install traceAI-haystack 'haystack-ai==3.4.1'
```

Set `FI_API_KEY` and `FI_SECRET_KEY` in your application's environment. Get these credentials from your Future AGI project settings.

## Configure and run

The existing `HaystackInstrumentor().instrument(tracer_provider=..., config=...)` entry point installs a single Haystack tracer. Do not combine it with another Haystack instrumentor or call `haystack.tracing.enable_tracing()` afterward: Haystack has one active tracer.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from haystack import Document, Pipeline
from haystack.components.preprocessors import DocumentCleaner
from traceai_haystack import HaystackInstrumentor

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="haystack-documents",
)
HaystackInstrumentor().instrument(tracer_provider=provider)

pipeline = Pipeline()
pipeline.add_component("clean", DocumentCleaner())
result = pipeline.run({
    "clean": {"documents": [Document(content="  Haystack   processes documents.  ")]}
})
print(result["clean"]["documents"][0].content)
provider.force_flush()
```

Open **haystack-documents** in Future AGI to inspect the `Pipeline.run` trace and its `DocumentCleaner.run` child span. For a short-lived process, flush the provider before exiting.

Haystack 3.x also supports `await pipeline.run_async(data)` and `pipeline.run_async_generator(data)` on the same pipeline. On Haystack 2.x, use `haystack.AsyncPipeline` for these methods. Fully consume or explicitly close asynchronous generators so their pipeline spans can finish.

## Content and privacy

Content capture is opt-in. By default, the adapter follows `HAYSTACK_CONTENT_TRACING_ENABLED` (false unless enabled). Model names and token counts remain available when Haystack provides them, without recording prompts, replies, documents, tool arguments/results, embedding vectors, or error details.

To inspect content, explicitly enable it when instrumenting:

```python
from fi_instrumentation import TraceConfig

HaystackInstrumentor().instrument(
    tracer_provider=provider,
    capture_content=True,
    config=TraceConfig(hide_embedding_vectors=True),
)
```

Choose one instrumentor configuration at startup; calling `instrument()` again does not reconfigure an already instrumented process. Call `uninstrument()` before changing the configuration. `TraceConfig` and the corresponding `FI_HIDE_*` settings take precedence over content capture. For example, `TraceConfig(hide_inputs=True, hide_outputs=True)` hides content even with `capture_content=True`.

## Coverage

- Pipelines emit one root span and one span per component invocation through Haystack's public `Tracer`/`Span` interface. No private execution hooks or component methods are patched.
- Public pipeline entry points carry component metadata into that tracer, preserving configured embedding/reranker models and prompt templates that Haystack's native tags do not include. These metadata bindings do not create spans.
- Generator components retain model, token, and message attributes; retrievers, rankers, and embedders retain their semantic attributes.
- Native agent and tool spans are captured where the installed Haystack version emits them. Haystack 2.10 does not emit the agent/tool events available in 3.4.1.
- Standalone component calls are captured only if the component itself emits native tracing. Put components in a pipeline to capture their execution automatically.
- `HaystackInstrumentor().uninstrument()` restores the previous Haystack tracer and the public entry points owned by this instrumentor.
