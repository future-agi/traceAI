## [Unreleased]
### Fixed
- LangGraph's `configurable.thread_id` is now copied to `session.id` on every span of the run (LangGraph puts it in run metadata). Before, it landed only in `thread_id`/`gen_ai.conversation.id`, so LangGraph and Deep Agents runs had no session unless wrapped in `using_session(...)`. An explicit `using_session(...)` or `metadata={"session_id": ...}` still wins.
- Token counts on chat model spans now come from the message's `usage_metadata` first, and the provider's raw `llm_output` usage only when it is missing. Anthropic's raw `input_tokens` leaves out cached prompt tokens, so with prompt caching (on by default in Deep Agents) an LLM span reported 4 input tokens for a 4018-token prompt.
- Failed runs no longer export a stack trace. LangChain's `run.error` is `repr(error)` plus the formatted traceback, which was copied into the span status description (local file paths, usernames), and `Span.record_exception` added `exception.stacktrace`. The status description and the `exception` event now carry only the exception type and message. With `TraceConfig(hide_inputs=True)` or `hide_outputs=True` the message is dropped too and only the exception type name is kept.

### Added
- Deep Agents cookbook (`examples/deep_agents.py`) and compatibility test (`tests/test_deepagents_compat.py`, skipped when `deepagents` is not installed). Tested with `deepagents==0.7.21`, `langchain-core` 1.6.6, `langgraph` 1.2.12. No new package and no new dependency.

## [0.2.0] - 2026-07-30
### Fixed
- `LangGraphInstrumentor` no longer breaks async graph nodes. The previous build monkey-patched `StateGraph.add_node`/`compile` and wrapped node functions with a sync wrapper, which returned a coroutine from `async def` nodes and raised `INVALID_GRAPH_NODE_RETURN_VALUE`. It also corrupted per-request state under concurrency and traced HITL `interrupt()` pauses as errors.
- LangGraph node, tool and LLM spans (plus graph-node enrichment via `gen_ai.agent.graph.node_name`/`node_id`, session grouping, and correct HITL interrupt handling) are now captured automatically by `LangChainInstrumentor`'s callback handler — no graph monkey-patching.
- HITL `interrupt()` is kept `OK` (not `ERROR`) for tool/LLM/retriever runs too, not only chain nodes; the graph-node attributes are emitted only on the node's own span.
- Implemented the `on_interrupt`/`on_resume` graph-lifecycle callbacks so langgraph 1.2+ no longer logs an `AttributeError` on every HITL turn.

### Changed
- **BREAKING:** `LangGraphInstrumentor` is now a deprecated no-op shim. It remains importable and `instrument()` is safe to call (it logs a one-time deprecation notice), but tracing is driven entirely by `LangChainInstrumentor`. You can remove the `LangGraphInstrumentor().instrument()` call.
- **BREAKING:** dropped the required `langchain` and `langchain-community` dependencies (the instrumentation only imports `langchain-core`). Pinning `langchain-community` transitively capped `langchain-core < 0.4` and blocked LangChain/LangGraph 1.x; the package now installs cleanly alongside `langchain`/`langgraph` 1.x without `--no-deps`.
- **BREAKING:** minimum Python raised to `>=3.10` (aligns with `langchain-core`/`langgraph` 1.x).

## [0.1.9] - 2025-06-10
### Feature
- Added support for ai-evaluation

## [0.1.8] - 2025-06-05
### Feature
- Bug Fixes
- Support for adding custom attributes through metadata

## [0.1.7] - 2025-05-29
### Feature
- Updated dependencies to the latest versions.

## [0.1.6] - 2025-05-23
### Feature
- Added support for FutureAGI's protect

## [0.1.5] - 2025-05-08
### Feature
- Updated dependencies to the latest versions.

## [0.1.4] - 2025-05-02
### Changed
- Enhanced image data extraction and support for OpenAI CUA.

## [0.1.3] - 2025-04-14
### Changed
- Updated dependencies to the latest versions.
- Enhanced attribute data extraction capabilities in LangChain, providing more efficient data handling.

