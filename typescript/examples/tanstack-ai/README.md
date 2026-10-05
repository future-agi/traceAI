# TanStack AI → Future AGI (recipe)

There is no `@traceai/tanstack-ai` package. TanStack AI's own `otelMiddleware`
creates the spans. This example registers a Future AGI tracer provider with
`@traceai/fi-core`, passes its tracer to `otelMiddleware`, and leaves content
capture off.

Pinned and tested: `@tanstack/ai` 0.64.0, `@tanstack/ai-openai` 0.26.0,
`@traceai/fi-core` 1.0.0, `@opentelemetry/api` 1.9.1, Node 20, 22 and 26.
`package.json` `overrides` also pins `@opentelemetry/sdk-trace-node` 2.11.0,
which pins `@opentelemetry/sdk-trace-base` and `@opentelemetry/sdk-trace`
2.11.0: the root usage move in `src/tracing.mjs` edits the SDK span's
attributes object, which is not public API, so re-run the tests before
changing that pin.
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

Copy `src/tracing.mjs` into your app. It wraps TanStack AI's
`otelMiddleware` (imported from the `@tanstack/ai/middlewares/otel` subpath,
not from `@tanstack/ai`) with the Future AGI span kinds, the root usage move
and the optional session, and registers the tracer provider with
`@traceai/fi-core`. Do not pass a bare `otelMiddleware` to `chat()`: its root
span repeats every model call's usage, so Future AGI would count it twice.
A route then looks like this:

```js
import { chat } from "@tanstack/ai";
import { openaiChatCompletions } from "@tanstack/ai-openai";
import {
  flushTraces,
  futureAgiOtelMiddleware,
  registerFutureAgiTracing,
} from "./tracing.mjs";

// Once per process. register() reads FI_API_KEY, FI_SECRET_KEY, FI_BASE_URL.
const tracerProvider = registerFutureAgiTracing();

export async function chatRoute(question, { threadId } = {}) {
  // captureContent stays at its default of false.
  const middleware = futureAgiOtelMiddleware(
    tracerProvider.getTracer("tanstack-ai"),
    { threadIdAsSession: Boolean(threadId) },
  );
  try {
    // Read the stream to the end, even after RUN_ERROR, so the spans end
    // and export. Do not use chat()'s non-streaming mode.
    let text = "";
    let runError = null;
    for await (const chunk of chat({
      adapter: openaiChatCompletions(process.env.OPENAI_MODEL ?? "gpt-4o-mini"),
      messages: [{ role: "user", content: question }],
      middleware: [middleware],
      ...(threadId ? { threadId } : {}),
    })) {
      if (chunk.type === "RUN_ERROR") runError ??= chunk;
      else if (chunk.type === "TEXT_MESSAGE_CONTENT") text += chunk.delta ?? "";
    }
    if (runError) throw new Error(runError.message);
    return text;
  } finally {
    // Waits at most 2 s and never throws. A long-lived server can drop
    // this flush: see Notes.
    await flushTraces(tracerProvider);
  }
}
```

`src/chat.mjs` is the runnable version of this route, with a tool and a
system prompt; it takes the tracer provider as an argument. The contract
test runs this snippet as written.

## What is traced

| Span | Name | Kind set by the recipe | Key attributes |
|---|---|---|---|
| chat() call | `chat <model>` | `AGENT` when it ran more than one model call, else none | `gen_ai.request.model`, `tanstack.ai.iterations`, and TanStack's sum of every model call's usage, renamed: each `gen_ai.usage.<suffix>` becomes `tanstack.ai.root_usage.<suffix>` and each `gen_ai.cost.<suffix>` becomes `tanstack.ai.root_usage.cost.<suffix>`. Future AGI sums `gen_ai.usage.*` over the trace, so the root's copy would double tokens, cache, reasoning and cost. The root keeps `gen_ai.usage.*` only when no model call reported usage. |
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

## Session

Pass the caller's conversation id as TanStack's `threadId`, and build the
middleware with `threadIdAsSession: true`. Every span then carries
`session.id` = that thread id, so Future AGI groups the conversation's traces:

```js
const middleware = futureAgiOtelMiddleware(tracer, { threadIdAsSession: true });
chat({ adapter, messages, threadId: conversationId, middleware: [middleware] });
```

`src/chat.mjs` takes it as a second argument:
`node src/chat.mjs "What is the weather in Paris?" conversation-42`.

Deviation from the traceAI context helper: `setSession()` from
`@traceai/fi-core` only sets an OpenTelemetry context value. Neither
`otelMiddleware` nor the plain SDK tracer that `register()`'s provider hands
out reads it, so it would not reach these spans. The
recipe uses `threadId` instead. Leave `threadIdAsSession` off when the caller
has no conversation id: `chat()` then generates a new `thread-<ms>-<random>`
id per call, and each request would become its own session.

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
- `forceFlush()` rejects when the collector is unreachable and can wait for
  the exporter's 10 s timeout when the collector accepts but does not answer.
  `flushTraces()` logs either case, waits at most 2 s (`FLUSH_TIMEOUT_MS`),
  and never throws, so `chat()` and the route still answer.
- The per-request flush is for serverless routes, where the process can be
  frozen after the response. In a long-lived server, do not await a flush on
  the request path: the default `SimpleSpanProcessor` exports each span when
  it ends. Call `shutdownTraces()` when the process stops.
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

It also runs `tests/span_kinds.test.mjs` with `node --test` (span kinds,
root usage move and session against fake spans and the pinned SDK span) and
the snippet under "The recipe" as written.

`tests/capture_content_on.mjs` is a test control that turns content capture
on, to prove the no-content check would catch a leak. `tests/timed_route.mjs`
and `tests/abort_mid_stream.mjs` are fixtures for the flush bound and the
abort case. None of them is part of the recipe.
