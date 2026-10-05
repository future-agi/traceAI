# Changelog

## 0.1.0

- First release. `FutureAGICallback`, a LangChain callback handler for
  `mcp_use.MCPAgent(callbacks=[...])`, records one `AGENT` span per agent
  run, one `LLM` span per model call and one `TOOL` span per tool call,
  parented as LangChain reports the run tree.
- Content (prompts, model output, tool arguments, tool results, error
  messages) is off by default; `capture_content=True` turns it on.
  `TraceConfig` `hide_inputs`, `hide_outputs` and `pii_redaction` apply to
  every text written, including error text.
- Secrets (values passed in `redact=`, secret-named environment variables,
  bearer tokens and key-shaped strings) are removed from everything written.
- Tested with mcp-use 1.7.1 on Python 3.11, 3.12 and 3.13.
