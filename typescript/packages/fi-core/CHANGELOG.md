## [1.2.2] - pending release
### Fix
- Preserve numeric `gen_ai.usage.cache_read_tokens.audio` counts through admission, projection and export. The 1.2.0 shared privacy runtime mistook this modality count for an audio payload. Only nonnegative safe integers receive the metric role; strings, arrays, media ancestry and registered secrets retain their existing privacy protections.

## [1.2.1] - pending release
### Fix
- **Spans no longer carry the process command line or the OS user.** 1.2.0 added OpenTelemetry's full process detector to every provider resource, so `process.command_args`, `process.command`, `process.owner`, `process.executable.path` and `process.pid` were exported with every span. Command-line flags can hold secrets. By default fi-core now detects only `process.runtime.name`, `process.runtime.version` and `process.runtime.description`, as it detected nothing before 1.2.0.
- To opt in, set the OpenTelemetry Node variable `OTEL_NODE_RESOURCE_DETECTORS` to a comma-separated list of `host`, `os` and `process`, or `all` / `none`. `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` are always applied.

## [1.2.0] - pending release
### Changed
- 1.2.0 exports the typed session.id set with setSession; other session-named attributes remain masked.
- With hideEmbeddingVectors, the legacy embedding.model_name attribute is also hidden.
- **Base64 images are no longer exported.** fi-core never exports base64 image payloads; they become `<omitted: N base64 chars>` at any cap. Before, the default cap of 32000 exported images up to 32000 characters.
- Default cap 0 redacts any value or token that mentions a data: image URL, including mentions with no payload.
- The replacement is `[image-redacted]`, for the whole value or JSON string token.
- `base64ImageMaxLength` and `FI_BASE64_IMAGE_MAX_LENGTH` keep working, with one shared default constant, `DEFAULT_BASE64_IMAGE_MAX_LENGTH` (0). With a positive value, image payloads are still omitted, and are replaced in place by the media markers rather than replacing the whole value.
- An invalid cap resolves to 0 and logs `Invalid base64 image cap; using 0.`
- The TypeScript SDK is stricter: it never exports base64 images at any cap.

### Feature
- The shared export-privacy runtime and `registerSecret(value)` that the new zero-code agent `@traceai/auto` uses. See the README.

### Fix
- Non-HTTP(S) URLs in text, such as `wss://`, `redis://` or `s3://`, are no longer mistaken for base64 audio and partly omitted. Media payload and credential masking are unchanged.
- A manual `batch: false`, set explicitly or by default, keeps a simple span processor. Only the zero-code agent batches, on its own export route.
- Safe URL query parameters are kept byte-exact, and credential values in URLs are masked by the shared sanitizer. Masking of assignments in prose and headers is unchanged.
- Hidden embedding-vector fallbacks use the `__REDACTED__` marker consistently.
- Native SDK count-limit evidence is kept for late library projection, and Genkit native loss evidence is kept before mapping rewrites.

### Known limitations
- embedding.model_name may be omitted from FI exports of very large embedding batches; use gen_ai.request.model.
- The image cap does not detect headerless base64 images (PNG, JPEG, GIF, WebP without a data: URL) unless they are the whole value or a whole JSON string token, nor a declared image payload whose base64 contains spaces; the media policy may still omit them.
- An over-cap image payload stored under a key outside the payload fields (data, bytes, content, image, image_url, url, base64, b64_json) of an image-declaring object, without a data: URL, is not detected by the image cap (e.g. {"type":"image","mime_type":"image/heic","blob":"<base64>"}); other media policy may still withhold it.

## [1.1.1] - pending release
### Fix
- Export the shared PII policy for native exporters. `@traceai/mastra` requires at least this version; publish fi-core before publishing Mastra.

## [1.1.0] - 2026-10-18
### Security fix: content hiding skipped the attributes passed when a span is created
- **This bug predates 1.1.0: it is in every published release, 0.1.0 to 1.0.0.** `FITracer.startSpan` handed `options.attributes` straight to the OpenTelemetry span, so attributes passed at span creation skipped `FISpan` and every masking rule: `hideInputs` and the other hide flags, PII redaction, and (new in this release) error-detail hiding. About 22 instrumentations put the prompt there, `@traceai/openai` included, so with `FI_HIDE_INPUTS=true` the prompt was still exported in `input.value`, `gen_ai.input.messages` and `raw.input`.
- `FITracer.startSpan` and `startActiveSpan` now run creation-time attributes through the same masking as `setAttribute(s)` (`maskAttributes`). The masked attributes are still passed at creation, so a sampler sees the same values that are exported. An `error.type` passed at creation is kept.
- Content hiding now also covers the raw bodies and template variables: `hideInputs` redacts `raw.input` and `llm.prompt_template.variables`, `hideOutputs` redacts `raw.output` (as `input.value` / `output.value`).
- To get the fix, an instrumentation package must resolve `@traceai/fi-core` 1.1.0. Packages published with `^1.0.0` get it on upgrade. `@traceai/openai`, `@traceai/anthropic`, `@traceai/bedrock` and `@traceai/langchain` were published with `^0.1.x` and need the new releases that depend on `^1.1.0`.

### Feature
- New `hideErrorDetails` trace config option (env `FI_HIDE_ERROR_DETAILS`). When on, an ERROR status keeps no description, and `exception.message`, `exception.stacktrace`, `error.message` and instrumentor keys ending in `.exception.message` / `.exception.stacktrace` / `.error.message` / `.error_message` are dropped from span attributes, exception events and other events. It is a denylist, as in the Python core: `exception.type`, `exception.escaped`, `error.type` and every other attribute are kept. `FISpan` enforces it for `recordException`, `setStatus`, `addEvent` and `setAttribute(s)`, and `FITracer` for creation-time attributes, so every instrumentation that goes through `FITracer` gets it.
- Tri-state: when neither the option nor the env var is set, `hideErrorDetails` is on whenever any of `hideInputs`, `hideOutputs`, `hideInputMessages`, `hideOutputMessages`, `hideInputText` or `hideOutputText` is on. An explicit `true` / `false` (option or env) wins.
- Spans that end in ERROR now carry `error.type`: the provider error code when the error has one (OpenAI-compatible `code` such as `model_not_found`, Anthropic's body `error.type` such as `not_found_error`, Node's `ECONNREFUSED`, OpenAI `type` when there is no code), otherwise the error class name, otherwise `_OTHER`. An `error.type` set by the instrumentation is kept.
- `exception.type` is the same in both modes: a string `code` that is an identifier (as the OpenTelemetry SDK records it), otherwise the error name / class. A numeric code such as an HTTP `400` is no longer recorded as the type (the SDK alone would record `"400"`); Python records the class too.
- Exports `generateTraceConfig`, `getErrorType`, `getExceptionType`, `getErrorMessage`, `stripStackTrace` and the error / exception attribute key constants.

### Fix
- An ERROR status description is the error message only: `FISpan.setStatus` strips JavaScript stack frames and relayed Python tracebacks, and reduces `"<Name>: <message>"` (`String(error)`) to the message of the recorded exception. A multi-line message is kept whole when the description is a full `err.stack`, and a relayed Python traceback keeps its final `Type: message` line.
- `FITracer.startActiveSpan` makes the `FISpan` the active span, so `trace.getActiveSpan()` inside the callback goes through masking and the error-detail policy instead of reaching the raw span.
- `FI_HIDE_*`, `FI_BASE64_IMAGE_MAX_LENGTH` and `FI_PII_REDACTION` env vars are now read when an instrumentation is created without a `traceConfig`. Before, they only applied when a `traceConfig` object was passed.

### Behaviour change
- Attributes passed at span creation are now masked (see the security fix). If you set a hide flag, values you saw before in `input.value`, `gen_ai.input.messages`, `raw.input`, `raw.output` or `llm.prompt_template.variables` are now redacted or dropped.
- If you set any of the content-hiding flags above (option or env), error messages, stack traces and status descriptions are now hidden as well. Set `hideErrorDetails: false` (or `FI_HIDE_ERROR_DETAILS=false`) to keep them.
- If you set `FI_HIDE_*` env vars without passing a `traceConfig`, they now take effect. So do `FI_PII_REDACTION` and `FI_BASE64_IMAGE_MAX_LENGTH`.
- Error spans get a new `error.type` attribute.
- Inside `FITracer.startActiveSpan`, `trace.getActiveSpan()` returns the `FISpan`, not the SDK span. Code that casts it to the SDK `Span` or `ReadableSpan` (e.g. to read `.attributes` or `.name`) no longer works there; use the span passed to the callback through the `Span` API instead.
- `FISpan.recordException` builds the exception event itself (same keys as the SDK) instead of calling the SDK's `recordException`.

## [0.1.10] - 2025-06-10
### Feature 
- Updated OpenTelemetry to v2.0.1
- Updated dependencies
- Bug Fixes

## [0.1.9] - 2025-05-26
- Bug Fixes

## [0.1.8] - 2025-05-26
- Bug Fixes

## [0.1.7] - 2025-05-23
- Bug Fixes

## [0.1.6] - 2025-05-23
- Bug Fixes

## [0.1.1] - 2025-05-20
- Dependencies Updated

## [0.1.0] - 2025-05-19
- Initial Release