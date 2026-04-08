# Changelog

All notable changes to `traceAI-a2a` will be documented here.

## [0.1.0] - 2026-04-08

### Added
- 🎉 Initial Release
- ✨ `A2AInstrumentor` for zero-config tracing of Google A2A Protocol SDK calls
- ✨ W3C TraceContext (`traceparent`/`tracestate`) propagation across agent boundaries
- ✨ `A2A_CLIENT` span kind for outbound agent calls
- ✨ `A2A_SERVER` ASGI middleware span kind for inbound A2A tasks
- ✨ Full semantic conventions: task ID, task state, agent URL, AgentCard name/version, message role, artifact type
- ✨ Async and sync `A2AClient` instrumentation
- ✨ SSE streaming support with per-artifact span events
- ✨ Complete example: two-agent distributed trace demo
