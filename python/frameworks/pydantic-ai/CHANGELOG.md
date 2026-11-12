## [0.2.0] - pending release

### Changed
- traceai-pydantic-ai now requires Python 3.10 or later. Action: Stay on the previous traceai-pydantic-ai release if you run Python 3.9.

### Fixed
- Instrumented run_stream works as an async context manager, the tracing content privacy settings apply to captured content, and spans use the standard span kinds and content attributes.
- Streamed agent runs (run_stream) record their input and, once the stream is consumed, their output, like non-streamed runs.
