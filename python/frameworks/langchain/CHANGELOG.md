## [0.2.0] - 2026-07-30
### Fixed
- `LangGraphInstrumentor` no longer breaks async graph nodes. The previous build monkey-patched `StateGraph.add_node`/`compile` and wrapped node functions with a sync wrapper, which returned a coroutine from `async def` nodes and raised `INVALID_GRAPH_NODE_RETURN_VALUE`. It also corrupted per-request state under concurrency and traced HITL `interrupt()` pauses as errors.
- LangGraph node, tool and LLM spans (plus graph-node enrichment via `gen_ai.agent.graph.node_name`/`node_id`, session grouping, and correct HITL interrupt handling) are now captured automatically by `LangChainInstrumentor`'s callback handler — no graph monkey-patching.

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

