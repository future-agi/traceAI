# OpenRouter Agent tracing

Trace `@openrouter/agent` through its official lifecycle hooks. No monkey-patching, second model invocation, or provider transport instrumentation.

## Install

Requires Node.js 22+ and Agent 0.11.x. Keep the SDK on its compatible 0.13.x line.

```sh
npm install @traceai/openrouter-agent @openrouter/agent@0.11.0 @openrouter/sdk@0.13.67
```

## Use

Set `FI_API_KEY`, `FI_SECRET_KEY`, `FI_PROJECT_NAME` and `OPENROUTER_API_KEY` in your application environment. For self-hosting, set `FI_BASE_URL` to the instance origin; the exporter adds `/tracer/v1/traces`.

```js
import { OpenRouter } from '@openrouter/sdk';
import { callModel } from '@openrouter/agent';
import { buildTracerProvider, createTraceSession } from '@traceai/openrouter-agent';

const provider = buildTracerProvider();
const client = new OpenRouter({ apiKey: process.env.OPENROUTER_API_KEY });
const telemetry = createTraceSession(provider);
try {
  const result = callModel(client, {
    model: 'openai/gpt-4o-mini',
    input: 'Explain what a trace is in one sentence.',
    hooks: telemetry.hooks,
  });
  console.log(await telemetry.getText(result));
} finally {
  telemetry.close();
  await provider.shutdown();
}
```

Reuse your provider in a server, but create one trace session per `callModel` invocation. An existing OTel `TracerProvider` works too. `buildTracerProvider()` does not register a global provider or change context management. The adapter captures the active parent context when you create it; you can also pass `parentContext` explicitly.

For streaming, replace `telemetry.getText(result)` with iteration over `telemetry.getTextStream(result)`. It forwards chunks without making extra calls or draining a stream you abandon. Use the upstream `signal` option to cancel work. `close()` marks abandoned tracing incomplete; it does not cancel the agent.

For state or approval resumes, create a new trace session for each invocation, pass its `hooks`, and consume through its `getText()` or `getTextStream()`. Upstream session identity links the separate traces when state exists. Do not consume a result before attaching the adapter, or reuse a trace session for another run.

## Recorded fields

- An AGENT span per invocation, LLM spans for completed model calls, TOOL spans for completed or failed tool executions.
- Model name, response ID, turn type/number, session ID when available, and usage including cache-read/reasoning tokens and cost when reported. Usage is not duplicated on the root.
- Tool input comes from the completion/failure event itself. Same-name calls, identical inputs, hook-mutated inputs and out-of-order completions cannot consume each other's queue entries: there is no queue.
- Successful tool timing comes from the completion event. Failure hooks have neither duration nor invocation ID; those spans set `openrouter.agent.duration_unavailable=true` rather than guess.

A rejected initial model request produces an error on the agent span, not a fabricated completed-model span. Blocked/paused tools do not emit a completed tool span until they execute. Work inside sub-agents, server-side tools, and cross-process deferred-tool settlement without lifecycle hooks is not instrumented by this adapter.

## Privacy

Content capture is **off by default**. Enable `FI_CAPTURE_CONTENT=true` or pass `{ captureContent: true }` to `createTraceSession()` to record the prompt, tool input/output, consumed answer and error messages. Names, IDs, duration, status and usage remain visible without content capture; do not put sensitive data in names or IDs.

`FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and `FI_HIDE_ERROR_DETAILS` override opt-in for their respective fields. Flags are read when creating the adapter. Content fields are limited to 32,000 characters plus a truncation marker. Error stacks and custom error names are not recorded, even with opt-in. Non-Error thrown objects are represented by a fixed label rather than serialized.

Model hooks contain no answer text. `getText()` attaches the final answer to the agent span before it ends. `getTextStream()` attaches the consumed text across turns only after successful exhaustion; incomplete streams do not claim a completed answer. Other upstream consumption APIs still work with hooks but do not gain automatic answer capture.

## Migrate from the copied helper

The package preserves the helper's `buildTracerProvider(providerOptions)`, `createTraceHooks(provider, options)` and `envCaptureContent()` entry points. Replace the import from your copied helper; do not install both hook sets. Use `createTraceSession()` for answer capture and approval-resume teardown.

If you register your own hooks for the same event, append the tracing entries instead of replacing them. Do not enable another wrapper that records the same model requests.
