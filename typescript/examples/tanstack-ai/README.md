# TanStack AI → Future AGI (recipe)

There is no `@traceai/tanstack-ai` package. TanStack AI's own `otelMiddleware`
creates the spans. This example registers a Future AGI tracer provider with
`@traceai/fi-core`, passes its tracer to `otelMiddleware`, and leaves content
capture off.

Pinned and tested: `@tanstack/ai` 0.64.0, `@tanstack/ai-openai` 0.26.0,
`@traceai/fi-core` 1.0.0, `@opentelemetry/api` 1.9.1, Node 20, 22 and 26.
`@tanstack/ai` is MIT and is a dependency of this example only. It is not
vendored and not a dependency of any traceAI package.

## Run

```bash
cd typescript/examples/tanstack-ai
npm install

export FI_API_KEY="YOUR_API_KEY"        # sent as X-Api-Key
export FI_SECRET_KEY="YOUR_SECRET_KEY"  # sent as X-Secret-Key
export FI_PROJECT_NAME="my-chatbot"     # resource attribute project_name
export OPENAI_API_KEY="YOUR_OPENAI_KEY"
# Optional: FI_BASE_URL (default https://api.futureagi.com), OPENAI_BASE_URL

node src/chat.mjs "What is the weather in Paris?"
```

`register()` exports OTLP/HTTP to `{FI_BASE_URL}/tracer/v1/traces` with the
`project_name` and `project_type=observe` resource attributes. The collector
drops a batch without `project_name`.

## The recipe

```js
import { register, ProjectType } from "@traceai/fi-core";
// Subpath import: otelMiddleware is not exported from "@tanstack/ai".
import { otelMiddleware } from "@tanstack/ai/middlewares/otel";

const tracerProvider = register({
  projectType: ProjectType.OBSERVE,
  projectName: "my-chatbot",
});

const middleware = otelMiddleware({
  tracer: tracerProvider.getTracer("tanstack-ai"),
  // captureContent defaults to false. Leave it unset.
});

// chat({ adapter, messages, middleware: [middleware] })
// then, in the route's finally: await tracerProvider.forceFlush()
```

`src/tracing.mjs` adds the span kinds and a flush that never throws.
`src/chat.mjs` is the route.

## What is traced

| Span | Name | Kind set by the recipe | Key attributes |
|---|---|---|---|
| chat() call | `chat <model>` | `AGENT` when it ran more than one model call, else none | `gen_ai.request.model`, `tanstack.ai.iterations`, summed usage as `tanstack.ai.root_usage.*` (Future AGI sums `gen_ai.usage.*` over the trace, so the root's copy would double it; kept on the root only when no model call reported usage) |
| model call | `chat <model> #<n>` | `LLM` | `gen_ai.operation.name=chat`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `tanstack.ai.iteration` |
| tool call | `execute_tool <name>` | `TOOL` | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type` |

Usage is a span attribute, not only a metric: `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, and, when the provider reports them,
`gen_ai.usage.total_tokens`, `gen_ai.usage.cost`,
`gen_ai.usage.cache_read.input_tokens`,
`gen_ai.usage.cache_creation.input_tokens` and
`gen_ai.usage.reasoning.output_tokens`.

The kind (`gen_ai.span.kind`) is keyed on the middleware's span scope. Only
model-call spans carry `gen_ai.operation.name`. The root's kind is set in
`onSpanEnd`, because `attributeEnricher` runs for the root before the number
of model calls is known.

## Privacy

`captureContent` defaults to `false`, so no prompt, completion, system prompt,
or tool argument or result reaches a span. Do not pass `captureContent: true`.
If you opt in, use `redact`: a redactor that throws emits `[redaction_failed]`,
never the raw text.

Keep `FI_API_KEY` and `FI_SECRET_KEY` on the server. Never import this code
into a browser bundle.

## Notes

- Spans are exported by `register()`'s default `SimpleSpanProcessor`.
  `register({ batch: true })` does not attach its batch processor with
  `@traceai/fi-core` 1.0.0 on `@opentelemetry/sdk-trace(-base)` 2.11.0 (the
  versions this example installs), so it is not passed.
- `forceFlush()` rejects when the collector is unreachable. `flushTraces()`
  logs that and returns, so `chat()` and the route still succeed.
- Do not also register the OpenInference TanStack middleware or wrap `chat()`.
  Either duplicates spans.

## Contract test

Runs the example against a loopback fake of the OpenAI Chat Completions API
and the shared harness receiver. No vendor API is called.

```bash
cd <repo root>
PYTHONPATH="python/tests" uv run --no-project --python 3.11 \
  --with pytest --with protobuf --with opentelemetry-proto \
  pytest typescript/examples/tanstack-ai/tests -q -p no:cacheprovider \
  --noconftest -o addopts=''
```

`tests/capture_content_on.mjs` is a test control that turns content capture
on, to prove the no-content check would catch a leak. It is not part of the
recipe.
