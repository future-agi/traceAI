# Changelog

## 0.1.0

- First release. `FutureAGICallback`, a LangChain callback handler for
  `mcp_use.MCPAgent(callbacks=[...])`, records one `AGENT` span per agent
  run (`run()`, `stream()` or `stream_events()`), one `LLM` span per model
  call and one `TOOL` span per tool call. LLM and tool spans are children
  of the agent span; LangGraph nodes and middleware are not spans. A
  streamed completion is one LLM span with chunk events. Nothing is
  patched: only agents given the callback are traced.
- Spans come from `FITracer`: `TraceConfig` and `using_session`,
  `using_user`, `using_metadata` and `using_tags` apply.
- Content (prompts, messages, model output, tool arguments and results,
  error messages) follows `TraceConfig` like every traceAI instrumentor:
  recorded by default, hidden with `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS`
  (and the message/text variants) or `config=TraceConfig(...)`. There is no
  package-specific switch. With either hide flag, error messages are
  dropped (exception type only). Under `hide_outputs`, model replies and
  tool results that `MCPAgent` memory or `external_history` sends back on a
  later turn are outputs too.
- Never recorded: stack traces (no `exception.stacktrace`; the `stack` of
  mcp-use's formatted tool error is cut from the tool message the model
  gets), absolute local paths, and this machine's user and host name.
- Secrets (values passed in `redact=`, secret-named environment variables,
  bearer tokens and key-shaped strings) are removed from everything written,
  names and ids included. Recorded text is size-capped.
- Failed tool calls are ERROR tool spans, including an MCP failure mcp-use
  returns as the tool's result (recognised by its `tool` key matching the
  tool, so a tool's own error-shaped result stays a result); a failed run
  is an ERROR agent span and the exception reaches the caller unchanged;
  cancelled runs end their spans as `cancelled`. Callback failures never
  reach the agent.
- Tested with mcp-use 1.7.1 on Python 3.11, 3.12 and 3.13.
