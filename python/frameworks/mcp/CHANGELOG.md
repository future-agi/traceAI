## [0.1.3] - 2026-10-31
### Fixed
- Support both streamable HTTP client names and the MCP SDK's two- and three-item transport tuples.
- Disable propagation on existing streams during teardown, restore owned hooks without replacing other libraries' wrappers, and reactivate existing instrumented streams after reinstall.
- Propagate context without mutating caller-owned messages or metadata, including null metadata; preserve original messages when there is no context to inject.
- Keep queued server messages and native transport errors intact across instrumentation transitions.

### Changed
- Align the runtime SDK dependency check with the declared MCP 1.8.1 minimum.

## [0.1.0] - 2025-06-05
### Feature
- Added support for MCP framework