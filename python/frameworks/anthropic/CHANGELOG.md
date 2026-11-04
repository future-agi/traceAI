## [0.1.12](https://github.com/future-agi/traceAI/compare/traceai-anthropic-v0.1.11...traceai-anthropic-v0.1.12) (2026-10-31)


### Bug Fixes

* **anthropic:** finalize stream telemetry once, outside the stream lock, with a bounded queue of pending events ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** keep Messages tracing working on SDKs without the legacy Completions API ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** preserve native stream iteration, partial close, context-manager results, errors and cancellation ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** report dropped stream telemetry without logging content ([#327](https://github.com/future-agi/traceAI/issues/327)) ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** require fi-instrumentation-otel&gt;=1.2.2 for its error-detail policy ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))
* **anthropic:** restore token-count hooks and accept mapping-shaped tool inputs ([8600a66](https://github.com/future-agi/traceAI/commit/8600a66c1a692f44a9f937640bb4c3ecbd42c98d))

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
