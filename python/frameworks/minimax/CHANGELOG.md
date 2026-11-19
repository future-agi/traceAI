## [0.1.1] - pending release

### Changed
- traceai-minimax now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- MiniMax requests that include a system message are traced. Previously their spans were silently dropped.
- Streamed MiniMax calls no longer raise a TypeError in your loop when chunks carry fields set to None, and the streamed span keeps its token usage when later chunks report none.
