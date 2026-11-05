# Changelog

## [0.1.3](https://github.com/future-agi/traceAI/compare/npm-traceai-vercel-v0.1.2...npm-traceai-vercel-v0.1.3) (2026-11-05)


### Bug Fixes

* **ci:** release already-merged SDK changes at bootstrap ([0a13942](https://github.com/future-agi/traceAI/commit/0a139428d589d3d5a2ebda0a250f1928efe4b9b8))
* **npm:** declare the runtime dependencies of @traceai/vercel and @traceai/bedrock ([1ead4ec](https://github.com/future-agi/traceAI/commit/1ead4ec15d15365cabea32d15085b5c48404d99b))
* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))
* **release:** cover core manifests and stage npm dependencies ([896d14e](https://github.com/future-agi/traceAI/commit/896d14eb530ea38d449f8a4b14b523530b17d2e2))
* **release:** unblock the first main release found by a dry run ([4965783](https://github.com/future-agi/traceAI/commit/49657838882374f3b19df5d6a874140aff28d784))

## [0.1.2](https://github.com/future-agi/traceAI/compare/npm-traceai-vercel-v0.1.1...npm-traceai-vercel-v0.1.2) (2026-11-01)


### Bug Fixes

* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to npm; one-time bootstrap patch for the automated release pipeline.
- Declare `@opentelemetry/sdk-trace-base` as a dependency. The span processor imports it at runtime, so a clean install without it failed with "Cannot find module".
