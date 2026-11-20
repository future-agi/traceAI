# traceAI-agentspec

OpenTelemetry instrumentation for [Oracle Open Agent Spec](https://github.com/oracle/agent-spec)
(`pyagentspec`, import `pyagentspec`).

```bash
pip install traceAI-agentspec
```

```python
from fi_instrumentation import register
from traceai_agentspec import AgentSpecInstrumentor

AgentSpecInstrumentor().instrument(tracer_provider=register(project_name="my-project"))
```

With `fi-instrumentation-otel` auto-instrumentation the `agentspec` integration is enabled whenever
`pyagentspec` is installed.

## How it works

`pyagentspec` has its own tracing API: a runtime opens a `Trace`, and the spans and events it emits
are handed to the `SpanProcessor`s of that trace. The instrumentor adds one such processor to every
`Trace` (the list you pass in is never modified; processors you add keep working) and translates the
spans and events into OpenTelemetry spans. Runtimes and adapters that emit `pyagentspec` spans
(for example WayFlow) are therefore traced without any change to your code. `pyagentspec` itself
makes no model calls, so there is no HTTP layer to instrument and nothing to double count.

## What is traced

| `pyagentspec` span | traceAI span | name |
| --- | --- | --- |
| `AgentExecutionSpan`, `SwarmExecutionSpan`, `ManagerWorkersExecutionSpan` | `AGENT` | `agentspec.agent.<name>` |
| `FlowExecutionSpan`, `NodeExecutionSpan` | `CHAIN` | `agentspec.flow.<name>`, `agentspec.node.<name>` |
| `LlmGenerationSpan` | `LLM` | `agentspec.llm.<model id>` |
| `ToolExecutionSpan` | `TOOL` | `agentspec.tool.<name>` |

* The tree follows the `pyagentspec` span nesting; the outermost spans join the OpenTelemetry span
  that is active when the `Trace` starts.
* LLM spans carry the provider, request model, temperature / top_p / max_tokens (and top_k, seed,
  penalties when set), response id, token usage, the prompt and the response, and the tool calls the
  model asked for. The endpoint URL and API key of the `LlmConfig` are never exported.
* A streamed generation is assembled from its chunk events; a stream that closes before the final
  response is marked `agentspec.stream.incomplete` and keeps the text received so far.
* Exceptions are recorded as `ERROR` with `error.type` (the class name); the message and stack
  trace that `pyagentspec` records are never exported, and the exception is not touched.
* Inputs and outputs follow the `TraceConfig` hide settings. `pyagentspec` marks prompts, outputs and
  tool payloads as sensitive; pass `mask_sensitive_information=True` to
  `instrument()` to withhold all of them regardless of `TraceConfig`.
* A span the runtime never ended is ended when its `Trace` closes and marked
  `agentspec.span.unfinished`.
* Tracing failures are logged at debug level and never reach your code.

Supported SDK range: `pyagentspec >= 26.1.0, < 27` (the first release with the tracing API).
