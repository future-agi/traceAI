

## [0.1.7] - pending release

### Changed
- traceai-smolagents now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- Streamed agent runs keep the run span open until the stream ends, so each step is nested under it, the run span records the final answer and token usage, and a stream closed before it finishes is not marked successful.

## [0.1.6] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.5 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

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
- Added support for SmolAgents Instrumentation
