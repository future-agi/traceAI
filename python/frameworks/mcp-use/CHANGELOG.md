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
  error messages and stack traces) is off by default; `capture_content=True`
  turns it on. `hide_inputs`, `hide_outputs` and `pii_redaction` also apply
  to error text. Under `hide_outputs`, model replies and tool results that
  `MCPAgent` memory or `external_history` sends back on a later turn are
  outputs too.
- Secrets (values passed in `redact=`, secret-named environment variables,
  bearer tokens and key-shaped strings) are removed from everything written,
  names and ids included. Recorded text is size-capped.
- Failed tool calls are ERROR tool spans; a failed run is an ERROR agent
  span and the exception reaches the caller unchanged; cancelled runs end
  their spans as `cancelled`. Callback failures never reach the agent.
- Tested with mcp-use 1.7.1 on Python 3.11, 3.12 and 3.13.
