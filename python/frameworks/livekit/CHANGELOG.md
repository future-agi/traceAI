## [0.1.2] - 2026-10-19
### Fixed
- `enable_http_attribute_mapping()` / `enable_grpc_attribute_mapping()` no longer drop the Future AGI exporter when LiveKit
  adds a span processor of its own (livekit-agents 1.8 adds a privacy filter), so traces are sent again. A processor added
  after `enable_*` now runs alongside the exporter instead of replacing it.

## [0.1.1] - 2026-03-10
### Feature
- Version bump for PyPI publish.

## [0.1.0]
### Feature
- Added support for LiveKit instrumentation.
