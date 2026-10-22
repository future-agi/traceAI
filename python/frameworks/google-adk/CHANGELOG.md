# Changelog

## Unreleased

- Allow Google GenAI 2.x alongside 1.x so current Google ADK versions can
  install with the instrumentor through normal dependency resolution.
- Support the modular model-call and parallel-tool tracing bindings in ADK
  2.12. Restore original LLM tracing bindings when disabling instrumentation,
  and prevent SDK-owned passthrough spans from ending a parent span.
- Add offline integration coverage for the real ADK model/tool loop, span
  attributes, token usage, error propagation and uninstrumentation.

## [0.1.5] - 2026-05-15

- Fix `instrument()` crash on `google-adk >= 1.32`. Patch targets and
  ADK-internal tracer wraps are now selected at runtime based on the
  installed ADK version; both pre-1.32 and 1.32+ layouts are supported
  by the same release.
- Fix stale `__version__` in `traceai_google_adk/version.py` (was
  `0.1.3` in the published `0.1.4` wheel).
