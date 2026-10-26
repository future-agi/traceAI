## [Unreleased]
### Fix
- Keep native agent token rollups under `agent_framework.usage.*` so Cloud counts each descendant model call once, instead of adding the agent summary again.
- Honor the standard `FI_HIDE_*` / `TraceConfig` settings (inputs, outputs, messages, tool arguments/results, system instructions).
- Never export raw exception text: ERROR spans keep only the exception class name; `exception` events drop message and stack trace.

## [0.1.0] - 2026-05-25
### Feature
- Added support for Microsoft Agent Framework instrumentation.
