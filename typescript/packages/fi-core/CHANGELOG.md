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