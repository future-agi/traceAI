# Changelog

## 0.1.0

- Hermes Agent plugin (`futureagi`): one trace per Hermes turn with model-call and tool-call spans,
  `session.id` from the Hermes session, subagent turns nested under the parent turn.
- Content (prompts, responses, tool arguments and results) off by default; `FI_HERMES_CAPTURE_CONTENT=true` opts in.
- Pip entry point `hermes_agent.plugins: futureagi` and a directory-plugin wrapper (`hermes-plugin/futureagi/`)
  for Hermes installs whose Python environment is managed by Hermes.

### Review fixes (before release)

- Error text is content: turn exit reasons are reduced to their category (`repeated_outer_errors`) and provider
  errors to Hermes' category and class unless `FI_HERMES_CAPTURE_CONTENT=true`.
- No longer calls `fi_instrumentation.register()`, which replaced Hermes' SIGINT/SIGTERM handlers: the plugin builds
  its own TracerProvider (uncompressed OTLP/HTTP, explicit keys) and never builds it from `on_session_finalize`.
- Multi-profile gateways: settings are read through Hermes' profile secret scope, one provider per profile.
- Retry attempts start at their own hook time instead of the logical call's `started_at`.
- Tool spans come from `post_tool_call` only; `pre_tool_call` (fail-closed policy hook) is no longer registered.
- Exit and teardown flushes are bounded (about 3 s when Future AGI is unreachable; at most about 6 s with
  OpenTelemetry 1.35, now the minimum, whose exporter retries a failed connection once).
- Turns that Hermes reports as not failed get status `OK` (the private provider no longer inherits
  `fi_instrumentation`'s OK default); turns closed at teardown or exit stay unset.
- Opt-in model input is the latest messages of the request, not the head of the body (the system prompt).
- `api_request_error` reads Hermes' `{"type", "message"}` error payload.
