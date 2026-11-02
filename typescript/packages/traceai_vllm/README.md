# @traceai/vllm

Trace calls from the OpenAI Node SDK to a vLLM server and send client spans to Future AGI. Chat Completions and legacy Completions include model, token usage, errors, and optionally request/response content.

This package traces the **client**, not the inference server. vLLM's native server-side OpenTelemetry is a complementary source of inference timing; it does not replace client payload capture. See [vLLM observability](https://docs.vllm.ai/en/stable/api/vllm/config/observability/) for server configuration.

## Install

```bash
npm install @traceai/vllm @traceai/fi-core openai
```

Use OpenAI SDK 4+ with a Node.js version supported by that SDK (this package requires Node.js 18+). You need a running vLLM OpenAI-compatible endpoint and its served model name. This instrumentor covers `chat.completions.create` and `completions.create`, not Responses, embeddings, or server metrics/logs.

## Send traces to Future AGI

Set your Future AGI credentials and vLLM connection settings:

```bash
export FI_API_KEY="<future-agi-api-key>"
export FI_SECRET_KEY="<future-agi-secret-key>"
export VLLM_BASE_URL="http://localhost:8000/v1"
export VLLM_MODEL="<served-model-name>"
# Set VLLM_API_KEY if your vLLM deployment requires authentication.
```

Save this as `trace-vllm.mjs`. The explicit module registration works with ESM and with an SDK imported before tracing setup.

```javascript
import * as openai from "openai";
import { register, ProjectType } from "@traceai/fi-core";
import { VLLMInstrumentation } from "@traceai/vllm";

const provider = register({
  projectName: "vllm-client",
  projectType: ProjectType.OBSERVE,
});

const instrumentation = new VLLMInstrumentation({
  baseUrlPattern: /^http:\/\/localhost:8000\/v1\/?$/,
  traceConfig: { hideInputs: true, hideOutputs: true },
});
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(openai);

const client = new openai.OpenAI({
  baseURL: process.env.VLLM_BASE_URL,
  apiKey: process.env.VLLM_API_KEY || "not-required",
});

try {
  const response = await client.chat.completions.create({
    model: process.env.VLLM_MODEL,
    messages: [{ role: "user", content: "Explain continuous batching in one sentence." }],
  });
  console.log(response.choices[0]?.message.content);
} finally {
  instrumentation.disable();
  await provider.shutdown();
}
```

```bash
node trace-vllm.mjs
```

Open the `vllm-client` project in Future AGI to inspect the `vLLM Chat Completions` span. The example hides prompt/response content and error details while retaining model, usage, duration, and status. To capture payloads, set `hideInputs` and `hideOutputs` to `false` only for data you intend to export.

For another server address, update **both** `VLLM_BASE_URL` and the anchored `baseUrlPattern`. Without a pattern, this package treats every matching OpenAI SDK call in the process as vLLM, including calls to other providers. A string pattern retains substring matching; prefer an anchored regular expression for endpoint isolation.

Use only one OpenAI client instrumentor in a process. Do not enable `@traceai/openai`, another provider-specific OpenAI instrumentor, and `@traceai/vllm` over the same SDK resources. A resource already wrapped by another instrumentor is left untouched.

## Streaming and response helpers

Within the example's `try` block, use `stream: true` and consume or explicitly cancel the stream:

```javascript
const stream = await client.chat.completions.create({
  model: process.env.VLLM_MODEL,
  messages: [{ role: "user", content: "Explain continuous batching in one sentence." }],
  stream: true,
  stream_options: { include_usage: true },
});
for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
```

The native stream object, controller, and SDK-provided helpers are retained. The span ends on exhaustion, iteration failure, early return, or explicit abort. Usage is captured only when the server returns it. Stream telemetry retains at most 64K characters of content/tool data and up to 128 choices/tool indexes; the application still receives every chunk unchanged. A stream that is neither consumed nor cancelled remains open.

`withResponse()` retains parsed data and native response metadata. `asResponse()` leaves the raw body untouched and ends the span at response headers, without output or usage attributes. Later raw-body consumption is not traced.

Legacy Completions work through the same setup:

```javascript
const response = await client.completions.create({
  model: process.env.VLLM_MODEL,
  prompt: "Continuous batching improves throughput because",
  max_tokens: 64,
});
```

## Configuration

| Option | Purpose |
| --- | --- |
| `baseUrlPattern` | String substring or regular expression matched against the SDK client's base URL. |
| `traceConfig` | FI content/error privacy options. Content capture remains enabled by default for compatibility; the example explicitly hides it. Input message/text/image or output message/text hiding conservatively omits the corresponding payload attributes (including raw bodies, tools, and invocation extras) because those aliases can duplicate private content; model and usage metadata remain available. |
| `instrumentationConfig` | OpenTelemetry instrumentation settings, including `enabled: false` for deferred activation. |

`disable()` restores this instrumentor's resource methods, including manually registered modules. `enable()` reapplies registered modules. CommonJS automatic instrumentation must be enabled before loading `openai`; for ESM or an already loaded SDK, use `manuallyInstrument()`.

## Troubleshooting

- **Requests succeed but no spans appear:** check the URL pattern against the full SDK base URL, explicitly register your imported module, and ensure no other instrumentor owns the resource methods.
- **Missing final stream span:** finish consuming the stream, break out of its loop, or call `stream.controller.abort()` before shutting down your provider.
- **Missing token counts:** configure your server to return usage; for supported streaming endpoints, request `stream_options.include_usage`.

## License

Apache-2.0
