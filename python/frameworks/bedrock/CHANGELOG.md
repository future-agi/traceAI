

## [0.1.12] - pending release

### Changed
- traceai-bedrock now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- converse_stream calls are instrumented.
- invoke_model_with_response_stream spans for non-Anthropic models end when the caller finishes or closes the stream, and record the model, plus output and token usage when the stream includes them.
- invoke_model calls with a file-like request body are accepted, and tracing leaves the body unread for the SDK.

## [0.1.11] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.10 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.8] - 2025-07-08
### Feature
- Added support for Bedrock's new multi-modal response format
- Updated dependencies to the latest versions.

## [0.1.7] - 2025-06-10
### Feature
- Added support for ai-evaluation

## [0.1.6] - 2025-05-29
### Feature
- Updated dependencies to the latest versions.

## [0.1.5] - 2025-05-23
### Feature
- Added support for FutureAGI's protect

## [0.1.4] - 2025-05-08
### Feature
- Updated dependencies to the latest versions.

## [0.1.3] - 2025-04-14
### Changed
- Updated dependencies to the latest versions.
