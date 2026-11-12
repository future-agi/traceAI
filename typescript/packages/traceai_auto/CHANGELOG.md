## [0.1.5] - pending release

### Fixed

- The bundled launcher (`@traceai/auto/preload.cjs`, which the server bundler plugins also load) includes @traceai/fi-core 1.2.5, so session ids from the application context are exported as set instead of "<redacted>", and spans from its built-in OpenAI instrumentation carry the FI context attributes of the active context. On a span that lost data before export (for example attributes dropped by the OpenTelemetry attribute limits, or a wire capture marked traceai.wire.truncated, traceai.wire.incomplete or traceai.wire.body_unavailable), session.id still reads "<redacted>". Upgrade @traceai/auto to 0.1.5, and replace any copied dist/agent directory, if you use preload.cjs or a bundler plugin (@traceai/auto/esbuild, /vite, /rollup, /webpack or /next). If you start your app with @traceai/auto/register or call init() from @traceai/auto, it uses the installed @traceai/fi-core, so upgrading @traceai/fi-core is enough.

## [0.1.4] - pending release

### Added
- Registry entries for `@traceai/n8n` (import hook on `n8n-core`), `@traceai/openrouter` and `@traceai/vitest-evals` (both manual setup), so `traceai-auto` reports them and the coverage table lists them.

### Fixed
- OpenAI-compatible Chat Completions and Responses calls captured from the global fetch, including streamed response.failed events, mark the span as an error when the provider returns a failure inside a successful HTTP response, with the provider's message when one is given. Calls made with the OpenAI SDK are traced by the OpenAI instrumentation instead.
- Gemini capture records every generated candidate, not only the first.

## [0.1.3] - pending release

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
