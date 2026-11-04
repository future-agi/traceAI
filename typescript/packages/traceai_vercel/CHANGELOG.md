# Changelog

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-vercel-v0.1.1...npm-traceai-vercel-v0.1.2) (2026-11-01)


### Bug Fixes

* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to npm; one-time bootstrap patch for the automated release pipeline.
- Declare `@opentelemetry/sdk-trace-base` as a dependency. The span processor imports it at runtime, so a clean install without it failed with "Cannot find module".
