# traceAI-promptflow

OpenTelemetry instrumentation for [Microsoft Prompt flow](https://github.com/microsoft/promptflow)
(`promptflow-tracing`, `promptflow-core`; import `promptflow`).

```bash
pip install traceAI-promptflow
```

```python
from fi_instrumentation import register
from traceai_promptflow import PromptflowInstrumentor

PromptflowInstrumentor().instrument(tracer_provider=register(project_name="my-project"))

# then run flows or call @trace functions as usual
```

With `fi-instrumentation-otel` auto-instrumentation the `promptflow` integration is enabled whenever
`promptflow` is installed.

Prompt flow ships with tracing off (`PF_DISABLE_TRACING` defaults to `true`). `instrument()` turns
it on unless the variable is already set, and `uninstrument()` puts it back. Setting
`PF_DISABLE_TRACING=true` yourself keeps Prompt flow tracing disabled.

## What is traced

Prompt flow already opens a span for each `@trace` function, DAG node or tool and flow run (flex
and DAG). This instrumentor routes those spans through traceAI instead of the global tracer
provider:

* the flow run, or an outermost `@trace` function, is a `CHAIN` span; nodes and nested `@trace`
  functions are child `TOOL` spans (`gen_ai.tool.name` is the function name). Calls made through
  Prompt flow's OpenAI integration are `LLM` spans with the response model and token usage;
  embedding vectors are never exported. `gen_ai.provider.name` is `promptflow` on every span;
* sync, async and generator results; a generator span ends on exhaustion, error, `close()` or
  when the generator is dropped. An early end is marked `promptflow.stream.incomplete`;
* errors are re-raised unchanged and recorded as `ERROR` with `error.type` (the class name); the
  message and traceback are never exported;
* inputs and outputs are exported as `input.value` / `output.value` and follow the `TraceConfig`
  hide settings. Prompt flow's own `inputs`, `output`, `prompt.template`, `prompt.variables` and
  `promptflow.*` events are never exported, whichever Prompt flow code path writes them (for
  example a Prompty call): the span drops them, so hiding works for them too. If an installed
  Prompt flow version lacks a content hook this package patches, one warning is logged and the
  content stays masked; if it lacks `start_as_current_span`, nothing is instrumented;
* LLM and embedding spans carry `gen_ai.request.model` and the request parameters (temperature,
  top_p, max tokens, penalties, seed, stop) when the call's inputs include them;
* a failure inside the instrumentation never reaches your call.

`PF_DISABLE_TRACING=false` is set process-wide, so child processes inherit it, but the patches are
not: a worker process has to call `instrument()` itself.

Limits: the patches live in the instrumenting process. Prompt flow batch runs that execute lines in
worker processes, and the serving `/feedback` endpoint (which opens its own span on the global
tracer provider), are not covered, so their spans do not follow the `TraceConfig` hide rules.

Do not also instrument the OpenAI SDK for the calls Prompt flow already traces: you would get a
second LLM span for each.

Supported SDK range: `promptflow-tracing >= 1.12.0, < 2`.
