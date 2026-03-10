# Changelog

All notable changes to traceAI-chromadb will be documented in this file.

## [0.1.0] - 2025-01-24

### Added
- Initial release
- Instrumentation for ChromaDB v0.4.x and v0.5.x
- Support for `add`, `query`, `get`, `update`, `upsert`, `delete`, `count`, `peek` operations
- OpenTelemetry semantic conventions for vector database operations
- Query type detection (embedding vs text)
- Result metrics (count, IDs, distances)
- Collection metadata tracking
