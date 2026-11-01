# Changelog

## [0.1.7](https://github.com/future-agi/traceAI/compare/traceai-google-adk-v0.1.6...traceai-google-adk-v0.1.7) (2026-11-01)


### Bug Fixes

* **google-adk:** use native tracing with transactional setup ([#445](https://github.com/future-agi/traceAI/issues/445)) ([b55153a](https://github.com/future-agi/traceAI/commit/b55153a4c9acae2cb45c8401d70bdf6cc7532bbd))

## [0.1.6] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.5 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.5] - 2026-05-15

- Fix `instrument()` crash on `google-adk >= 1.32`. Patch targets and
  ADK-internal tracer wraps are now selected at runtime based on the
  installed ADK version; both pre-1.32 and 1.32+ layouts are supported
  by the same release.
- Fix stale `__version__` in `traceai_google_adk/version.py` (was
  `0.1.3` in the published `0.1.4` wheel).
