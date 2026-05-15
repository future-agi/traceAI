# Changelog

## [0.1.5] - 2026-05-15

- Fix `instrument()` crash on `google-adk >= 1.32`. Patch targets and
  ADK-internal tracer wraps are now selected at runtime based on the
  installed ADK version; both pre-1.32 and 1.32+ layouts are supported
  by the same release.
- Fix stale `__version__` in `traceai_google_adk/version.py` (was
  `0.1.3` in the published `0.1.4` wheel).
