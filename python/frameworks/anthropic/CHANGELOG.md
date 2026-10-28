## [Unreleased]
### Fixed
- `AnthropicInstrumentor().instrument()` raised `ImportError` on `anthropic>=1.0`
  and instrumented nothing. `anthropic` 1.0 removed the legacy Completions API
  (`anthropic.resources.completions`) entirely; `_instrument()`/`_uninstrument()`
  imported it unconditionally, so the whole method aborted before it got to wrap
  Messages. Completions wrapping is now skipped (with old `anthropic<1`
  installs unaffected) instead of failing instrumentation altogether.

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
