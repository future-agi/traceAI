## [Unreleased]

### Changed
- Defers to the shared OpenAI instrumentor (traceai-openai) while it is on, so a call yields one span, and declares `DEFERS_TO_SHARED_OPENAI`, which the core reads to turn the integration on by default (an older release without it stays off by default, so a call never yields two spans). Deferral needs a traceai-openai that labels MiniMax hosts.

## [0.1.1] - 2026-11-12

### Changed
- traceai-minimax now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

### Fixed
- MiniMax requests that include a system message are traced. Previously their spans were silently dropped.
- Streamed MiniMax calls no longer raise a TypeError in your loop when chunks carry fields set to None, and the streamed span keeps its token usage when later chunks report none.
