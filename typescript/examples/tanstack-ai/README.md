# TanStack AI → Future AGI (recipe)

There is no `@traceai/tanstack-ai` package. TanStack AI's own `otelMiddleware`
creates the spans. This example registers a Future AGI tracer provider with
`@traceai/fi-core`, passes its tracer to `otelMiddleware`, and leaves content
capture off.

Pinned: `@tanstack/ai` 0.69.0 (the latest release on 2026-10-16),
`@tanstack/ai-openai` 0.30.0, `@traceai/fi-core` 1.0.0, `@opentelemetry/api`
1.9.1, and through `overrides` `@tanstack/openai-base` 0.14.0: TanStack's
adapter packages move in lockstep with `@tanstack/ai` (each `openai-base`
minor asks for a newer `@tanstack/ai`), so an unpinned install can mix
versions this example was not tested with. `package-lock.json` (generated on
Node 22 against this set) pins every transitive version; `npm ci` installs
it. Also tested: the earlier pinned set, `@tanstack/ai` 0.64.0 with
`@tanstack/ai-openai` 0.26.0, `@tanstack/openai-base` 0.12.2 and the SDK at
2.11.0 (`otelMiddleware` is byte-identical from 0.64.0 to 0.69.0). Node
`>=20.6.0`, the floor of the pinned OpenTelemetry SDK on the Node 20 line.
The contract test and `node --test` suite (see "Contract test") pass on Node
22.18.0 and 26.8.1.
`package.json` `overrides` also pins `@opentelemetry/sdk-trace-node` 2.12.0,
which pins `@opentelemetry/sdk-trace-base` and `@opentelemetry/sdk-trace`
2.12.0: the root usage move in `src/tracing.mjs` edits the SDK span's
attributes object, which is not public API, so re-run the tests before
changing that pin.
`@tanstack/ai` is MIT and is a dependency of this example only. It is not
vendored and not a dependency of any traceAI package.

## Run

```bash
cd typescript/examples/tanstack-ai
npm ci   # installs the exact tested set from package-lock.json

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
not from `@tanstack/ai`) with the Future AGI span kinds, the root usage move,
the optional session, error privacy and a new trace per chat, and registers
the tracer provider with `@traceai/fi-core`. It needs `@opentelemetry/api`,
which `@tanstack/ai` already asks for as a peer. Do not pass a bare
`otelMiddleware` to `chat()`: its root span repeats every model call's
usage, so Future AGI would count it twice, and it exports error text and
stack traces.

The SDK pin does not travel with `src/tracing.mjs`: it lives in this
example's `package.json` `overrides`
(`"@opentelemetry/sdk-trace-node": "2.12.0"`). Copy that entry into your
app's `package.json` too (npm reads `overrides`; pnpm and Yarn have their own
field, `pnpm.overrides` and `resolutions`). Without it your app resolves the
SDK through `@traceai/fi-core`'s `^2.0.1` range, and the root usage move is
tested only on 2.11.0 and 2.12.0. If an SDK version stopped exposing the
span's plain `attributes` object, the move would do nothing, silently, and
Future AGI would count every model call's usage twice.

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

// `signal` is the request's abort signal: `request.signal` in a Fetch API
// handler, or an AbortController you abort on `res.on("close")` in node:http.
export async function chatRoute(question, { threadId, signal } = {}) {
  // captureContent stays at its default of false.
  const middleware = futureAgiOtelMiddleware(
    tracerProvider.getTracer("tanstack-ai"),
    { threadIdAsSession: Boolean(threadId) },
  );
  // When the client disconnects, abort chat() so its spans end as cancelled
  // and export. Without this chat() never learns the client left.
  const abortController = new AbortController();
  const abort = () => abortController.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  try {
    // Read the stream to the end, even after RUN_ERROR, so the spans end
    // and export. Do not use chat()'s non-streaming mode.
    let text = "";
    let runError = null;
    for await (const chunk of chat({
      adapter: openaiChatCompletions(process.env.OPENAI_MODEL ?? "gpt-4o-mini"),
      messages: [{ role: "user", content: question }],
      middleware: [middleware],
      abortController,
      ...(threadId ? { threadId } : {}),
    })) {
      if (chunk.type === "RUN_ERROR") runError ??= chunk;
      else if (chunk.type === "TEXT_MESSAGE_CONTENT") text += chunk.delta ?? "";
    }
    if (runError) throw new Error(runError.message);
    return text;
  } finally {
    signal?.removeEventListener("abort", abort);
    // Waits at most 2 s and never throws. A long-lived server can drop
    // this flush: see Notes.
    await flushTraces(tracerProvider);
  }
}
```

Never stop reading `chat()`'s stream without aborting its `abortController`:
a `break`, an early `return` or a dropped response body ends no span, so the
whole trace is lost. TanStack ends the spans only when the run finishes,
fails or is aborted. When you stream to the client with
`toServerSentEventsResponse(stream, { abortController })`, pass it the same
controller you passed to `chat()`, so a closed response aborts the run.

`src/chat.mjs` is the runnable version of this route, with a tool and a
system prompt; it takes the tracer provider as an argument. The contract
test runs this snippet as written.

## What is traced

| Span | Name | Kind set by the recipe | Key attributes |
|---|---|---|---|
| chat() call | `chat <model>` | `AGENT` when it ran more than one model call, else `CHAIN` (a root without a kind shows as type "unknown" in Future AGI) | `gen_ai.request.model`, `tanstack.ai.iterations`, and TanStack's sum of every model call's usage, renamed: each `gen_ai.usage.<suffix>` becomes `tanstack.ai.root_usage.<suffix>` and each `gen_ai.cost.<suffix>` becomes `tanstack.ai.root_usage.cost.<suffix>`. Future AGI sums `gen_ai.usage.*` over the trace, so the root's copy would double tokens, cache, reasoning and cost. The root keeps `gen_ai.usage.*` only when no model call reported usage. |
| model call | `chat <model> #<n>` | `LLM` | `gen_ai.operation.name=chat`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.provider.name`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `tanstack.ai.iteration` |
| tool call | `execute_tool <name>` | `TOOL` | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type` |

Usage is a span attribute, not only a metric: `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, and, when the provider reports them,
`gen_ai.usage.total_tokens`, `gen_ai.usage.cost`,
`gen_ai.usage.cache_read.input_tokens`,
`gen_ai.usage.cache_creation.input_tokens` and
`gen_ai.usage.reasoning.output_tokens`.

Provider: TanStack sends the adapter name as `gen_ai.system` (`openai-chat`
for `openaiChatCompletions`). The recipe adds `gen_ai.provider.name`, which
Future AGI reads first, on the chat and model-call spans: `openai` /
`openai-chat` → `openai`, `anthropic` → `anthropic`, `gemini` /
`gemini-text-interactions` → `google`, `grok` → `xai`, `groq` → `groq`,
`ollama` → `ollama`. Other adapters (OpenRouter, your own) get none. Future
AGI prices each model call from `gen_ai.request.model` and its tokens, so a
model its price table lists gets a cost whatever the provider.

The kind (`gen_ai.span.kind`) is keyed on the middleware's span scope. Only
model-call spans carry `gen_ai.operation.name`. The root's kind is set in
`onSpanEnd`, because `attributeEnricher` runs for the root before the number
of model calls is known.

A failed model call or tool is an ERROR span with `error.type` (see
"Privacy"); a tool failure also sets `tanstack.ai.tool.outcome=error`, and
an aborted run ends every open span as ERROR `cancelled` with
`tanstack.ai.completion.reason=cancelled`.

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

The thread id is copied verbatim to `session.id` on every span and leaves
your process. Use an opaque id, such as a random UUID per conversation, not
an email address, a user name or anything else that identifies a person.
The same goes for `chat()`'s legacy `conversationId` option: TanStack uses it
as the thread id when `threadId` is not set.

Deviation from the traceAI context helper: `setSession()` from
`@traceai/fi-core` only sets an OpenTelemetry context value. Neither
`otelMiddleware` nor the plain SDK tracer that `register()`'s provider hands
out reads it, so it would not reach these spans. The
recipe uses `threadId` instead. Leave `threadIdAsSession` off when the caller
has no conversation id: `chat()` then generates a new `thread-<ms>-<random>`
id per call, and each request would become its own session.

## Privacy

Content capture is off by default: no prompt, completion, system prompt, tool
argument or result, and no error message reaches a span. A stack trace never
does, in either mode.

- Messages and tools: `captureContent` (TanStack's own option) stays at its
  default of `false`.
- Errors: TanStack records a failed model call or tool with
  `span.recordException(error)` and the error's message as the span status,
  whatever `captureContent` is. That text comes from the provider or from
  your tool (a file path, an id, a masked key), and the exception event
  carries the stack trace with local paths and the user name. The recipe
  hands `otelMiddleware` a tracer whose spans replace both
  (`futureAgiErrorPrivacy` in `src/tracing.mjs`, public Tracer and Span API
  only): the span gets `error.type`, which is the provider's error code
  (`400`, `model_not_found`, `context_overflow`) or the error's class name;
  the status message and the `exception` event carry only that type. An
  aborted run keeps TanStack's fixed status text `cancelled`.

Opt in to content with `captureContent: true`. TanStack then exports
messages, tool arguments and results (`gen_ai.input.messages`,
`gen_ai.output.messages`, message events), and the recipe exports the
error's message too, still without a stack trace. Pass `redact` to rewrite
each text first; a redactor that throws yields `[redaction_failed]`, never
the raw text:

```js
const middleware = futureAgiOtelMiddleware(tracer, {
  captureContent: true,
  redact: (text) => text.replace(/\b[\w.+-]+@[\w-]+\.[\w.]+\b/g, "[email]"),
});
```

Keep `FI_API_KEY` and `FI_SECRET_KEY` on the server. Never import this code
into a browser bundle.

## Apps with their own OpenTelemetry setup

TanStack starts its chat span under whatever span is active. In an app with
its own provider and context manager (HTTP server instrumentation, a
framework's built-in tracing), that is the app's request span, which is
exported elsewhere. Future AGI would then hold a trace whose root has a
parent it never received, and the Tracing list would not show it. The
recipe starts every chat in a new trace (`root: true` from
`onBeforeSpanStart`). If your app's own spans also go to Future AGI and you
want the chat nested under them, pass `joinActiveTrace: true`.

## Notes

- Spans are exported by `register()`'s default `SimpleSpanProcessor`.
  `register({ batch: true })` does not attach its batch processor with
  `@traceai/fi-core` 1.0.0 on `@opentelemetry/sdk-trace(-base)` 2.11.0 or
  2.12.0 (`addSpanProcessor` fills a list the SDK 2.x provider never reads),
  so it is not passed.
- `forceFlush()` rejects when the collector is unreachable and can wait for
  the exporter's 10 s timeout when the collector accepts but does not answer.
  `flushTraces()` logs either case, waits at most 2 s (`FLUSH_TIMEOUT_MS`),
  and never throws, so `chat()` and the route still answer. Pass
  `flushTraces(tracerProvider, { timeoutMs })` for a different bound.
- The per-request flush is for serverless routes, where the process can be
  frozen after the response. The bound stops the wait, not the export: spans
  still pending when it runs out keep exporting in the background, and are
  lost if the runtime then freezes or exits. Raise `timeoutMs` if your
  platform allows the time.
- In a long-lived server, do not await a flush on the request path: the
  default `SimpleSpanProcessor` exports each span when it ends, in its own
  OTLP request (4 requests for a chat with one tool call). The OTLP/HTTP
  exporter allows 30 exports in flight and waits up to 10 s for each; with a
  slow collector, spans past that limit are dropped with "Concurrent export
  limit reached", from about 8 concurrent chats of 4 spans. Call
  `shutdownTraces()` when the process stops.
- Do not also register the OpenInference TanStack middleware or wrap `chat()`.
  Either duplicates spans.
- Future AGI's Tracing list shows a trace's tokens from its root span, while
  the trace view, sessions and dashboards sum every span. The recipe moves
  the root's usage to `tanstack.ai.root_usage.*` so those sums count each
  model call once; the list's Tokens cell for a TanStack trace is therefore
  empty. Per-call tokens and cost are on the LLM spans.
- With content capture on, the messages are span attributes
  (`gen_ai.input.messages`, `gen_ai.output.messages`): Future AGI shows them
  in the span's attributes, not in its Input and Output panes.

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
root usage move, session, the new-trace root and the error privacy tracer
against fake spans and the pinned SDK span) and the snippet under "The
recipe" as written.

`tests/capture_content_on.mjs` turns on the documented content opt-in, to
prove it works and that the no-content check would catch a leak.
`tests/tool_error.mjs` runs a tool that throws an error carrying a marker
and a file path, with content off and on. `tests/app_parent.mjs` calls the
route under an app's own active span, with and without `joinActiveTrace`.
`tests/timed_route.mjs`
and `tests/abort_mid_stream.mjs` are fixtures for the flush bound and the
abort case. `tests/disconnect_route.mjs` serves one HTTP request with the
route (the README snippet's or `src/chat.mjs`'s) and aborts its signal when
the client disconnects mid-stream. None of them is part of the recipe.
