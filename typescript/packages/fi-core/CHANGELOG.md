## [1.1.0] - 2026-10-18
### Feature
- New `hideErrorDetails` trace config option (env `FI_HIDE_ERROR_DETAILS`). When on, an ERROR status keeps no description, exception events keep only `exception.type` (and `exception.escaped`), and `exception.message`, `exception.stacktrace`, `error.message` and instrumentor keys ending in `.error.message` / `.error_message` / `.exception.message` / `.exception.stacktrace` are dropped from span and event attributes. `FISpan` enforces it for `recordException`, `setStatus`, `addEvent` and `setAttribute(s)`, so every instrumentation that goes through `FITracer` gets it.
- Tri-state: when neither the option nor the env var is set, `hideErrorDetails` is on whenever any of `hideInputs`, `hideOutputs`, `hideInputMessages`, `hideOutputMessages`, `hideInputText` or `hideOutputText` is on. An explicit `true` / `false` (option or env) wins.
- Spans that end in ERROR now carry `error.type`: the provider error code when the error has one (OpenAI-compatible `code` such as `model_not_found`, Anthropic's body `error.type` such as `not_found_error`, Node's `ECONNREFUSED`, OpenAI `type` when there is no code), otherwise the error class name, otherwise `_OTHER`. An `error.type` set by the instrumentation is kept. Exception events keep the `exception.type` the OpenTelemetry SDK records, in both modes.
- Exports `generateTraceConfig`, `getErrorType`, `getExceptionType`, `getErrorMessage`, `stripStackTrace` and the error / exception attribute key constants.

### Fix
- An ERROR status description is the error message only: `FISpan.setStatus` strips JavaScript stack frames (and relayed Python tracebacks), and reduces `"<Name>: <message>"` (`String(error)`) to the message of the recorded exception.
- `FITracer.startActiveSpan` makes the `FISpan` the active span, so `trace.getActiveSpan()` inside the callback goes through masking and the error-detail policy instead of reaching the raw span.
- `FI_HIDE_*`, `FI_BASE64_IMAGE_MAX_LENGTH` and `FI_PII_REDACTION` env vars are now read when an instrumentation is created without a `traceConfig`. Before, they only applied when a `traceConfig` object was passed.

### Behaviour change
- If you set any of the content-hiding flags above (option or env), error messages, stack traces and status descriptions are now hidden as well. Set `hideErrorDetails: false` (or `FI_HIDE_ERROR_DETAILS=false`) to keep them.
- If you set `FI_HIDE_*` env vars without passing a `traceConfig`, they now take effect.
- Error spans get a new `error.type` attribute.

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