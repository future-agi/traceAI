## [Unreleased]
### Fix
- Honor the standard `FI_HIDE_*` / `TraceConfig` settings (inputs, outputs, messages, tool arguments/results, system instructions).
- Never export raw exception text: ERROR spans keep only the exception class name; `exception` events drop message and stack trace.

## [0.1.0] - 2026-05-25
### Feature
- Added support for Microsoft Agent Framework instrumentation.
