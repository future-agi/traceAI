## [0.4.1] - 2026-11-19

### Changed
- Depends on `@traceai/fi-core` 1.4.1, whose `hideInputs` / `hideOutputs` masking matches the Python SDK's `TraceConfig`.

## [0.4.0] - 2026-11-19

### Added
- Amazon Bedrock Agents are traced zero-code: the `bedrock` integration also activates `@traceai/bedrock` when `@aws-sdk/client-bedrock-agent-runtime` is installed, including when it is the only Bedrock SDK in the app, so `InvokeAgent` and `InvokeInlineAgent` calls are traced (#727). This needs `@traceai/bedrock` 0.2.0 or later.

## [0.3.0] - 2026-11-18

### If you upgrade from 0.2.x
- **Without an explicit sample rate, the agent's own provider follows `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG`** (#711). When no `sampleRate`, `FI_SAMPLE_RATE` or file `sample_rate` is set, the default is `ParentBased(TraceIdRatio(1))`, so AI spans under an unsampled parent are now dropped with it. To keep 0.2.0's behaviour, set `FI_SAMPLE_RATE` (for example `1`). Invalid sampler names or ratio arguments produce one agent warning and use the default. Application-owned samplers and explicit-rate record-only flags are unchanged.

### Added
- Sampled spans on agent-created providers carry the numeric `fi.sampling.ratio` attribute (`0 < r < 1`) when the agent makes a fractional local ratio decision or inherits a valid ratio from a sampled parent's `fi=r:<ratio>` tracestate entry (#713). Local fractional decisions also write `fi=r:<ratio>` in W3C tracestate, so children in other processes inherit the trace-wide ratio. An absent attribute means `1.0`. Roots sampled locally at `1`, record-only spans, unsampled parents, invalid parent ratios and application-owned providers are not stamped.

### Changed
- `@traceai/auto` depends on exactly `@traceai/fi-core` 1.4.0, which keeps `fi.sampling.ratio` on exported spans under attribute limits.

### Fixed
- An AI call made while your app handles an incoming request that its sampler dropped (a non-recording server span under an unsampled remote parent) keeps that request's trace ID and parent, so it no longer starts a separate orphan trace (#715). Once the request's span ends, later work goes back to the parent that was active before it.

## [0.2.0] - 2026-11-17

### If you upgrade from 0.1.x
- **The agent no longer takes over your app's OpenTelemetry.** It registers no global tracer provider, context manager or propagator, and an OpenTelemetry SDK your app starts registers normally. If your code created spans through OpenTelemetry's global API without starting an SDK of its own, those spans now go nowhere: start an SDK (for example `NodeSDK`) in your app.
- **Only AI spans are sent to Future AGI.** Your app's HTTP, database, cache and custom spans stay with your own provider. To send them to Future AGI as well, set `FI_FORWARD_APP_SPANS=true`; this needs an OpenTelemetry SDK in your app. Without forwarding, incoming HTTP spans are sent only when `FI_INSTRUMENTATIONS` lists `http`, and only for a request that made an AI call.
- **AI spans no longer reach your app's own provider and exporters.** In 0.1.x, when your app had its own tracer provider, the agent created AI spans on it, so your own exporters received them too; now they are created on the agent's own provider and go only to Future AGI. To create them on your provider again, set `FI_FORWARD_APP_SPANS=true`.
- **Genkit spans are no longer sent by default,** and Genkit is reported as `manual`: Genkit creates its spans with the global tracer provider, which the agent no longer registers. To send them, set `FI_FORWARD_APP_SPANS=true`, or use the manual setup in `@traceai/genkit`'s README.
- **Listing `http` beside your app's own tracer provider no longer installs the agent's HTTP instrumentation;** the agent warns once and leaves incoming HTTP to your OpenTelemetry setup. To have the agent install it there, set `FI_FORWARD_APP_SPANS=true`.
- **A privacy switch with an unrecognised value now hides.** `FI_HIDE_*` and `FI_PII_REDACTION` accept `true`, `1`, `yes`, `on` and `false`, `0`, `no`, `off`, in any case. Any other non-empty value hides that content with one warning; before, only `true` hid. `FI_HIDE_ERROR_DETAILS` set to `off`, `no` or `0` now shows error details, as `false` did. To keep content visible, use one of the accepted values.
- **An empty `FI_HIDE_*` or `FI_PII_REDACTION` variable no longer masks the config file's value:** it counts as unset, so the file's setting applies, and an empty `FI_HIDE_ERROR_DETAILS` beside the file's `false` now shows error details even with a content switch on. To override the file, set the variable to an accepted value.
- **`FI_SAMPLE_RATE` now decides by trace id for every AI span on the agent's own provider, the default,** and your app's own sampler no longer drops AI spans. In 0.1.x a provider your app owned kept its sampler for them; with `FI_FORWARD_APP_SPANS=true`, AI spans are created on your provider and keep its sampler. If your app samples with a trace-id ratio sampler, set `FI_SAMPLE_RATE` to the same ratio: the same traces are kept where your sampler decides at the root of a trace, but not where it follows an incoming parent's decision.

### Changed
- `@traceai/auto` depends on exactly the `@traceai/fi-core` version it is built with, 1.3.0.
- AI spans keep their parent from your app's active span. A span is active only inside its own SDK call, so a request your app sends beside a call is captured as its own wire span.

### Known limitations
- Without an application context manager, a call's own request can also
  arrive as a wire span beside its AI span for BeeAI, the OpenAI Agents
  SDK, the Vercel AI SDK 7, Genkit and ElevenLabs. BeeAI and the OpenAI
  Agents SDK start spans in the SDK's own event handlers or trace
  processor, so the agent cannot tie the SDK's request to its span
  without guessing. The Vercel AI SDK 7 and Genkit are turned on through
  their own enable entry, outside the agent's call scope. ElevenLabs
  patches namespace getters without the agent's scope and starts its
  span outside a call cell. The duplicate-wire-span fix needs
  `@traceai/groq` 0.1.2 or later, `@traceai/mistral` 0.1.2 or later and
  `@traceai/portkey` 0.1.3 or later; with older versions of these packages,
  the call's own request can still arrive twice. Portkey's Responses API
  delegates to the `openai` package: when the agent's OpenAI integration
  is also on and the app has no context manager, OpenAI starts its own
  span too. The request produces two nested AI spans and no wire span.
  These are duplicate-span limitations, not lost calls.
- LangChain is not traced by the agent yet. Its model requests are still recorded: by the provider SDK's integration when one is on, otherwise as wire spans.

## [0.1.4] - 2026-11-12

### Added
- Registry entries for `@traceai/n8n` (import hook on `n8n-core`), `@traceai/openrouter` and `@traceai/vitest-evals` (both manual setup), so `traceai-auto` reports them and the coverage table lists them.

### Fixed
- OpenAI-compatible Chat Completions and Responses calls captured from the global fetch, including streamed response.failed events, mark the span as an error when the provider returns a failure inside a successful HTTP response, with the provider's message when one is given. Calls made with the OpenAI SDK are traced by the OpenAI instrumentation instead.
- Gemini capture records every generated candidate, not only the first.

## [0.1.3] - 2026-11-09

### Added
- Experimental W3C trace-context propagation for outgoing `node:http` / `node:https` requests only. The adapter is off by default and requires explicit `propagateOutgoingAdapters: true` or `TRACEAI_PROPAGATE_OUTGOING_ADAPTERS=true`; destination policy alone does not enable it. No HTTP CLIENT spans are created.
- Destination policy, FI-vetted traceparent/tracestate/existing baggage, application-header ownership, suppression and native transport-authority checks. Early raw headers and pristine unsent Expect queues retain body/framing/accounting behavior; reports contain no header values.

### Safety
- Shared-catalog vendor hosts and excluded effective ports (including `11434`) are unconditional no-addition gates; neither `all` nor matching positive host rules can override them.
- Observed native cross-origin redirects fence retained Context/span/baggage identities and observed span descendants before application response listeners run. Attributable callback/await follow-ups and return redirects remain closed; same-origin and fresh unrelated requests stay eligible. Explicitly detached identities and bypassed response hooks cannot be correlated. No agent follow-up requests are created, and application-owned headers remain untouched.
- Native redirect checks resolve Location against the proven serialized request URL with WHATWG URL and reject ambiguous same-scheme references. Strict HTTP origins retain terminal DNS dots, so dotted/undotted transitions close both directions and return hops. Shared case/IDNA/single-dot policy normalization still enforces every vendor/port exclusion.
- Baggage uses ASCII-safe W3C token/octet/property validation and percent encoding. Invalid metadata/members are omitted; every generated field is checked before normal serialization or early raw/Expect append, preventing Unicode-to-latin1 control aliases and serializer failures.

### Limitations
- Outbound propagation for fetch/Undici is not supported yet: no F4 adapter for global `fetch`, `undici.fetch` or `undici.request`. Existing AI global-fetch wire capture and transport credential observation are unchanged.
- Only guarded stock HTTP/HTTPS agent routes are eligible; unknown/custom/proxy routes fail closed. Runtime qualification is still required on the deployed Node version.

## [0.1.2] - 2026-11-08
### Fix
- **Upgrade.** The standalone agent bundles the fi-core privacy runtime. 0.1.0 and 0.1.1 bundled fi-core 1.2.0 and 1.2.2, which could export short base64 media payloads that carry no recognized file signature: type- or modality-declared video (`type: video`, `type: input_video`, `modality: video`) at any `FI_BASE64_IMAGE_MAX_LENGTH`, including the default 0; type-declared images when the cap was positive; and images whose declaration is a separate attribute from the payload, at any cap. 0.1.2 is rebuilt against fi-core 1.2.3, which omits all of these at any cap. Setting the cap to 0 is not a workaround.
- The npm package now includes the Apache-2.0 `LICENSE` file.

### Known limitations
- As in fi-core 1.2.3: Image data nested inside a JSON-encoded string field (for example an image url inside a JSON string) may still be exported at any image cap, including the default. A structural fix is planned for a later release. Upgrade anyway: 0.1.0 and 0.1.1 (bundling fi-core 1.2.0 and 1.2.2) export this shape and more (typed video at every cap, flattened image parts at cap 0).

## [0.1.1] - 2026-11-08
- New README and documentation; the standalone agent bundles fi-core 1.2.2.

## [0.1.0] - 2026-11-07
- First release: zero-code Node auto-instrumentation (adapters off by default), with bundled third-party notices.
