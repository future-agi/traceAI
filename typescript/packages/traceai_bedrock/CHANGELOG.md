## [0.1.4] - pending release

### Fixed
- Traced send() keeps its options and callbacks, response streams are read only as your code reads them, the requested model id is recorded in full, and streamed tool arguments from Anthropic models are recorded, with a placeholder when the argument JSON is incomplete or malformed.

## [0.1.1] - 2026-10-18
### Fix
- Declare `@aws-sdk/client-bedrock-runtime` `^3.693.0` as a peer dependency, like the other instrumentors declare their SDK. The package loads it at runtime, so a clean install without it failed with "Cannot find module".
- A failure that is not an `Error` of this realm (an error from another realm, a thrown string or a plain object) is recorded like any other error: an exception event, `error.type`, and a status description that is the message only. Before, the status was `String(error)` (`"TypeError: msg"` or `"[object Object]"`) with no exception event.
- Depends on `@traceai/fi-core` 1.1.0, which masks the attributes passed at span creation and adds `hideErrorDetails`. 0.1.0 was published against `@traceai/fi-core` `^0.1.16`.

## [0.1.3](https://github.com/future-agi/traceAI/compare/npm-traceai-bedrock-v0.1.2...npm-traceai-bedrock-v0.1.3) (2026-11-05)


### Bug Fixes

* correct registry metadata tests and standalone tracing recipes ([0f797ea](https://github.com/future-agi/traceAI/commit/0f797ea6f3c4fe3e4f636ea5a8ceb4677e35c4c6))
* **npm:** declare the runtime dependencies of @traceai/vercel and @traceai/bedrock ([1ead4ec](https://github.com/future-agi/traceAI/commit/1ead4ec15d15365cabea32d15085b5c48404d99b))
* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))
* **release:** unblock the first main release found by a dry run ([4965783](https://github.com/future-agi/traceAI/commit/49657838882374f3b19df5d6a874140aff28d784))

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-bedrock-v0.1.1...npm-traceai-bedrock-v0.1.2) (2026-11-01)


### Bug Fixes

* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))

## [0.1.0]
- Initial release.
