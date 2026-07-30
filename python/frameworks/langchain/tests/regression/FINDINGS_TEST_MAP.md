# LangGraph tracing — regression suite ↔ audit findings

This suite is the acceptance metric for the callback-first fix: each **RED** test
flags an audit finding by asserting the *correct* behaviour, and currently fails on
the buggy code. Each **GREEN** test locks in behaviour that already works so the fix
can't regress it. After the callback-first fix lands, the whole suite should be green.

Run (customer stack: langchain-core 1.0.2 / langgraph 1.0.1 / langgraph-prebuilt 1.0.8):

```
pytest tests/regression/ -o asyncio_mode=auto
```

Baseline on **unfixed** `origin/main`: **9 failed (RED, intended), 6 passed (GREEN), 1 skipped (opt-in slow)**.

**Status: fixed.** With the callback-first fix applied, the whole regression suite is green
(**16 passed, 1 opt-in slow skipped**) and the full package suite passes (**215 passed, 8
integration deselected**). The "Status now" column below describes the *unfixed* baseline —
i.e. what each test flags.

## Layer split

- **`test_callback_path.py`** — only `LangChainInstrumentor` (the supported / post-fix path).
- **`test_langgraph_instrumentor_defects.py`** — both instrumentors (customer's real setup, handoff §4); this is where the node-wrapping defects live.
- **`test_dependency_pins.py`** — packaging (handoff §5.4 / Area 4).

## Mapping

| Test | Finding | Status now | Fails because / Guards |
|---|---|---|---|
| `test_async_node_does_not_crash` | **#1** async node → `INVALID_GRAPH_NODE_RETURN_VALUE` (the reported bug) | RED | Sync `NodeWrapper` returns an un-awaited coroutine → `InvalidUpdateError` |
| `test_node_with_store_injection_does_not_crash` | **#3** `functools.wraps` leaks node signature | RED | LangGraph injects `store=` into a wrapper that takes only `(state, config)` → `TypeError` |
| `test_execution_counter_not_shared_across_conversations` | **#2** shared singleton state across conversations | RED | 2nd conversation's node span is `langgraph.node.agent[2]` (counter never reset) |
| `test_invoke_emits_no_nested_stream_span` | **#5** `invoke`→`stream` double span | RED | A plain `invoke()` emits a spurious nested `langgraph.stream` span |
| `test_reinstrumentation_does_not_break_graph_build` | **#7** singleton `__init__`-rerun nulls originals | RED | 2nd instantiation → `add_node` calls `None(...)` → `'NoneType' object is not callable` |
| `test_graph_node_enrichment_attribute_present` | **Area 1** missing graph-node enrichment | RED | `gen_ai.agent.graph.node_name` not mapped from `langgraph_node` metadata |
| `test_interrupt_node_span_is_not_error` | **#4** HITL interrupt traced as error | RED | `GraphInterrupt` reaches `on_chain_error` → node span `status=ERROR` |
| `test_langchain_pin_admits_1x` | **§5.4** dependency pin | RED | `langchain ^0.3.9` (`<0.4`) rejects `1.0.2` |
| `test_langchain_community_pin_admits_1x` | **§5.4** dependency pin | RED | `langchain-community ^0.3.9` rejects `1.0.2` |
| `test_resolves_with_langchain_1x_without_no_deps` | **§5.4** end-to-end resolve | SKIP (opt-in `FI_RUN_SLOW=1`) | Real resolve of the package + 1.x without `--no-deps` |
| `test_sync_node_emits_span_named_by_node` | callback path delivers node spans | GREEN | Guards: node → chain-run span named by node |
| `test_async_node_traces_without_crash` | callback path handles async (contrast #1) | GREEN | Guards: async node runs + is traced via callbacks |
| `test_tool_node_emits_tool_span` | **#8** tool tracing via callbacks | GREEN | Guards: ToolNode tool runs → `TOOL` spans without the LangGraph instrumentor |
| `test_session_id_propagates_from_using_session` | session = thread_id (§6.4) | GREEN | Guards: `using_session` → `session.id` on spans |
| `test_concurrent_ainvoke_isolated_span_trees` | concurrency isolation (contrast #2) | GREEN | Guards: two forced-interleaved conversations → two disjoint traces |
| `test_uninstrument_restores_stategraph_methods` | **#7** lifecycle | GREEN* | Guards: `uninstrument()` restores `StateGraph.add_node`/`compile` |

\* Passes today in isolation; the autouse `_hermetic_stategraph` fixture keeps it (and every test) order-independent, since the monkey-patch mutates `StateGraph` globally.

## Post-verification hardening (added after an adversarial multi-agent review)

A multi-agent review confirmed the fix is correct end-to-end, and surfaced three
gaps that are now closed:

| Test | Guards | Note |
|---|---|---|
| `test_real_error_still_marks_span_error` | Area 2 must not suppress *real* errors | A node raising `ValueError` must still yield an ERROR span + exception event. Proven effective: broadening `_is_graph_interrupt` to `return True` makes this test fail. |
| `test_async_interrupt_node_span_is_not_error` | #4 on the customer's **async** path | Async `interrupt()` via `ainvoke` → node span OK + `langgraph.interrupt`. |
| `test_node_enrichment_not_on_child_llm_span` | Area 1 enrichment scoping | Canonical `gen_ai.agent.graph.node_name` is emitted only on the node's own span (`run.name == langgraph_node`), not on nested LLM/tool child runs. |

Fixed-tree baseline is now **19 passed, 1 opt-in slow skipped** (regression suite);
**218 passed** for the full package suite.

## Findings intentionally not given a dedicated failing test

- **#6 stream span leak on partial drain** — reproducing a *leaked* (never-closed) span
  deterministically requires forcing generator finalization/GC timing; it is covered
  indirectly (the shim removes stream wrapping entirely, asserted by #5). Add a
  targeted leak test if the fix keeps any stream wrapping.
- **#9 `entry_point` never resolves**, **#10 reducer-ignoring diff**, **#11 `wrap_condition`
  async**, **#12 single-arg `add_node`** — cosmetic / latent / out of the critical cluster
  (see the fix plan). They fall away with node/graph wrapping removed; add tests if any
  wrapping is retained.

## Environment

The customer's exact stack, with the package installed `--no-deps` to avoid the
`langchain[all]` extras explosion:

```
python -m venv .tvenv && . .tvenv/bin/activate
pip install fi-instrumentation-otel langchain-core==1.0.2 langgraph==1.0.1 \
            langgraph-prebuilt==1.0.8 opentelemetry-sdk wrapt pytest pytest-asyncio
pip install --no-deps -e python/frameworks/langchain
```
