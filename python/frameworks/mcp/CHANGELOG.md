## [0.1.3] - 2026-10-31
### Fixed
- Support both streamable HTTP client names and the MCP SDK's two- and three-item transport tuples; avoid duplicate propagation when legacy aliases delegate to the new entry point.
- Disable propagation on existing streams during teardown, restore owned hooks without replacing other libraries' wrappers, and reactivate existing instrumented streams after reinstall.
- Propagate context without mutating caller-owned messages or metadata, including null metadata; preserve original messages when there is no context to inject.
- Keep queued server messages and native transport errors intact across instrumentation transitions.

### Changed
- Align the runtime SDK dependency check with the declared MCP 1.8.1 minimum.

## [0.1.4](https://github.com/future-agi/traceAI/compare/traceai-mcp-v0.1.3...traceai-mcp-v0.1.4) (2026-11-05)


### Bug Fixes

* **mcp:** require a cancellation-safe MCP SDK floor ([#576](https://github.com/future-agi/traceAI/issues/576)) ([f5c6ea8](https://github.com/future-agi/traceAI/commit/f5c6ea85f1d27de11a5dc10f54ee2c21d3628b7f))


### Documentation

* **mcp:** distinguish native tracing from context propagation ([#537](https://github.com/future-agi/traceAI/issues/537)) ([9efd48b](https://github.com/future-agi/traceAI/commit/9efd48b0aec40e5c54b9762084f8c29f6b26f4c6))

## [0.1.0] - 2025-06-05
### Feature
- Added support for MCP framework
