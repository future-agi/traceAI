## [0.2.1] - 2026-10-26
### Fixed
- LangGraph's `configurable.thread_id` is copied to `session.id` on every span, including Deep Agents subagents. Metadata `session_id` and `using_session(...)` retain precedence; blank thread ids set nothing.
- Chat model token counts prefer the message's `usage_metadata`, so cached prompt tokens are included; provider `llm_output` is a fallback.
- Failed-run status descriptions use the original exception message, never LangChain's traceback-bearing `run.error`. Stacktraces remain only in exception events when error details are visible. Non-`Exception` errors retain their own traceback and fully qualified type.

### Changed
- Core `fi-instrumentation-otel>=1.2.0` owns error hiding, including derived content-hide flags and an explicit `hide_error_details=False` override. Hidden failures retain their type but no status description, exception message or stacktrace. On older supported core versions, `hide_inputs` or `hide_outputs` applies the same hiding as a compatibility fallback.
- Failed-run output attributes (including raw output, messages and function calls) are omitted when error details are hidden: provider error bodies and response headers can echo private content. Session mapping and token usage are retained.

### Added
- Deep Agents cookbook (`examples/deep_agents.py`) and compatibility tests against the published `deepagents==0.7.21` wheel. No new package or runtime dependency.

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

