## [0.2.0] - 2026-09-24
### Fixed
- `LlamaIndexInstrumentor().instrument()` no longer crashes with `ModuleNotFoundError: llama_index.core.base.agent` on llama-index-core 0.13 and later.
- Streaming query and chat engines (`as_query_engine(streaming=True)`, `stream_chat`, `astream_chat`) no longer return an empty stream when tracing is on. The instrumentor used to serialize the streaming response object, which consumed its generator before the caller could read it. The streamed text is recorded on the LLM and `write_response_to_history` spans once the stream finishes.
- Workflow agents (`FunctionAgent`, `ReActAgent`, `CodeActAgent`, `AgentWorkflow`) are traced with span kind `AGENT` instead of `CHAIN`.
- Streaming chat no longer logs an `AttributeError` when the stream ends.
- Unhandled llama-index events are logged once per event type instead of on every event.
- Spans report the package's real version (was `0.1.0`).
- Removed pydantic v2 deprecation warnings on every LLM call.

### Changed
- **BREAKING:** minimum `llama-index-core` raised to `0.12.3`. Earlier versions lack `ChatMessage.blocks`, which the instrumentor reads on every chat call.
- The "ai-evaluation is not installed" notice is logged when `instrument()` runs instead of at import.
- README quickstart and examples use `FunctionAgent`; `OpenAIAgent` and the query-pipeline text-to-SQL example relied on APIs removed from llama-index.

## [0.1.7] - 2025-06-10
### Feature
- Added support for ai-evaluation

## [0.1.6] - 2025-05-29
### Feature
- Updated dependencies to the latest versions.

## [0.1.5] - 2025-05-23
### Feature
- Added support for FutureAGI's protect
- Bug fixes

## [0.1.4] - 2025-05-08
### Feature
- Updated dependencies to the latest versions.

## [0.1.3] - 2025-04-14
### Changed
- Updated dependencies to the latest versions.
- Enhanced attribute data extraction capabilities in LLama Index, providing more efficient data handling.