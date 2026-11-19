

## [0.1.8] - pending release

### Changed
- traceai-openai-agents now requires fi-instrumentation-otel 1.2.2 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- Response span output no longer repeats the request's instructions or input, and a Responses call's token usage is counted once.
- Responses with custom tool calls are traced, and their spans close even when converting their attributes fails.

## [0.1.7] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.6 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.3] - 2025-05-29
### Feature
- Updated dependencies to the latest versions.

## [0.1.2] - 2025-05-23
### Feature
- Added support for FutureAGI's protect

## [0.1.1] - 2025-05-08
### Feature
- Updated dependencies to the latest versions.

## [0.1.0] - 2025-04-14
### Features
- Added support for OpenAI Agents Instrumentation
