# Changelog

## 0.1.0

- Hermes Agent plugin (`futureagi`): one trace per Hermes turn with model-call and tool-call spans,
  `session.id` from the Hermes session, subagent turns nested under the parent turn.
- Content (prompts, responses, tool arguments and results) off by default; `FI_HERMES_CAPTURE_CONTENT=true` opts in.
- Pip entry point `hermes_agent.plugins: futureagi` and a directory-plugin wrapper (`hermes-plugin/futureagi/`)
  for Hermes installs whose Python environment is managed by Hermes.
