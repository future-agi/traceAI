## [0.2.1] - 2026-10-18
### Fixed
- Agent, team and tool spans were created with a raw OpenTelemetry tracer, so the
  instrumentor's `TraceConfig` never applied to them. They now go through `FITracer`.

### Changed
- With `fi-instrumentation-otel>=1.2.0`, the content hide flags (`hide_inputs`,
  `hide_outputs`, ...) also hide error details on AutoGen spans: no status description,
  no `exception.message` / `exception.stacktrace`, no `autogen.error.message` /
  `autogen.tool.error_message`. `error.type` is set. This is a behaviour change for users
  of those flags; see the core 1.2.0 changelog. Upgrade both packages together.
- The `ERROR` status description is now the exception message only. It used to end up as
  `"<Type>: <message>"`, set by OpenTelemetry's `use_span` when the error is re-raised.

## [0.1.3] - 2025-05-29
### Feature
- Updated dependencies to the latest versions.

## [0.1.2] - 2025-05-23
### Feature
- Added support for FutureAGI's protect

## [0.1.1] - 2025-05-08
### Feature
- Updated dependencies to the latest versions.


## [0.1.0] - 2025-04-14
### Features
- Added support for Autogen Instrumentation