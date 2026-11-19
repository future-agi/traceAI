## Unreleased

### Added
- Traces the Anthropic clients for Amazon Bedrock (`@anthropic-ai/bedrock-sdk`) and Google Vertex AI (`@anthropic-ai/vertex-sdk`): `messages.create` and streaming produce the same LLM spans as the base client, with `gen_ai.provider.name` set to `aws.bedrock` or `gcp.vertex_ai`.

### Fixed
- Patching the hosted clients builds no client with live credentials: the probe used to find the Messages class carries placeholder keys and a stub auth object, makes no request, reads no credential source and starts no credential discovery, so loading the SDKs cannot leave a rejected promise behind.
- With the base SDK and a hosted client loaded together (they share one Messages class), `@traceai/auto` reports the anthropic integration as instrumented instead of manual.

### Changed
- `init()` now returns an array of module definitions (the base SDK plus the two hosted-client packages) instead of one definition. `init()` is a protected method of the instrumentation base class; a subclass that overrides it or reads its result needs to accept an array.
- Raw input and raw output are not recorded while any hide setting is on, and an input hide setting records only response-side fields as output.

## [0.1.6] - 2026-11-12

### Fixed
- Manually configured tracing stops when disabled and resumes when re-enabled, separate copies of the Anthropic SDK loaded in one process can each be traced, and non-streamed output messages keep the text around tool calls.

## [0.1.3] - 2026-10-18
### Fix
- Content hiding now applies to the prompt passed when the span is created (`gen_ai.input.messages`, `raw.input`), via `@traceai/fi-core` 1.1.0, which this release depends on. 0.1.2 was published against `@traceai/fi-core` `^0.1.13`, which exported those attributes unmasked.
- Error spans follow `hideErrorDetails` and get `error.type` (from `@traceai/fi-core` 1.1.0).

## [0.1.5](https://github.com/future-agi/traceAI/compare/npm-traceai-anthropic-v0.1.4...npm-traceai-anthropic-v0.1.5) (2026-11-05)


### Bug Fixes

* **anthropic:** keep the legacy SDK APIPromise and stop marking aborted streams OK ([#506](https://github.com/future-agi/traceAI/issues/506)) ([8d2ef1b](https://github.com/future-agi/traceAI/commit/8d2ef1b391d638df52ec1e09b543ad2f49084ca8))
* **anthropic:** preserve response lifecycles and cache token usage ([#556](https://github.com/future-agi/traceAI/issues/556)) ([d46aeb4](https://github.com/future-agi/traceAI/commit/d46aeb409ed44635db89675c157a0773489e0f58))

## [0.1.4](https://github.com/future-agi/traceAI/compare/npm-traceai-anthropic-v0.1.3...npm-traceai-anthropic-v0.1.4) (2026-11-01)


### Bug Fixes

* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))

## [0.1.1] - 2025-06-10
### Feature
- Dependencies Updated


## [0.1.0] - 2025-06-10
### Feature
- Initial Release
- Added support for Anthropic
