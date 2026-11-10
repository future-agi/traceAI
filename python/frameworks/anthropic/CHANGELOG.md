## [0.1.12](https://github.com/future-agi/traceAI/compare/traceai-anthropic-v0.1.11...traceai-anthropic-v0.1.12) (2026-10-31)


### Bug Fixes

* **anthropic:** finalize stream telemetry once, outside the stream lock, with a bounded queue of pending events ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** keep Messages tracing working on SDKs without the legacy Completions API ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** preserve native stream iteration, partial close, context-manager results, errors and cancellation ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** report dropped stream telemetry without logging content ([#327](https://github.com/future-agi/traceAI/issues/327)) ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** require fi-instrumentation-otel&gt;=1.2.2 for its error-detail policy ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** restore token-count hooks and accept mapping-shaped tool inputs ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))

## [0.1.13](https://github.com/future-agi/traceAI/compare/traceai-anthropic-v0.1.12...traceai-anthropic-v0.1.13) (2026-11-05)


### Bug Fixes

* **anthropic:** preserve response lifecycles and cache token usage ([#556](https://github.com/future-agi/traceAI/issues/556)) ([d46aeb4](https://github.com/future-agi/traceAI/commit/d46aeb409ed44635db89675c157a0773489e0f58))
* **anthropic:** trace each call once when the SDK already traces it natively ([#507](https://github.com/future-agi/traceAI/issues/507)) ([5ae2b9b](https://github.com/future-agi/traceAI/commit/5ae2b9b4a3678af52abc01d90e4fe2fe1d4c2099))

## [0.1.11] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.10 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

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
