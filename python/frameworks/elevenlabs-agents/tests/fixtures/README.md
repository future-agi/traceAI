# Fixtures

Built from the documented shape on
https://elevenlabs.io/docs/eleven-agents/customization/opentelemetry-traces (re-read
2026-10-15) and the attribute names the Langfuse cookbook reads from real exports
(https://langfuse.com/integrations/no-code/elevenlabs, `elevenlabs.tool.params`,
`elevenlabs.producing_llm`, `elevenlabs.llm_usage.<model>.<kind>`, `elevenlabs.turn.index`).

Except `live_get_conversation_opentelemetry.json`, they are **not** captured from a live
ElevenLabs workspace. That one is a sanitized real `GET ?format=opentelemetry` body captured
2026-10-17 (ids replaced, clock-derived variables dropped).

`{{MARKER}}` is replaced at load time with a unique string so tests can prove that
no content reaches an exported span when the standard `FI_HIDE_*` switches are on, and
that it does by default. A few values ElevenLabs is not documented to send
(`host.name` and `process.executable.path` on the resource, a `project_name` on the
resource, a stack trace event, a local path and host name in a status message, an
email in a dynamic variable) are adversarial extras for the never-exported and
project-binding tests.

| File | What it is |
|---|---|
| `post_call_transcription_otel.json` | Webhook body: greeting, two user turns, two LLM turns, one tool call, one unknown span name, a signed media URL |
| `post_call_transcription_otel_error.json` | Abnormal disconnect: root status ERROR, failing tool with an exception event, interrupted agent turn |
| `post_call_transcription_otel_no_text.json` | Turns without text, zero-length root with `time_in_call_secs` |
| `get_conversation_opentelemetry.json` | `GET /v1/convai/conversations/{id}?format=opentelemetry` body |
| `other_webhook_types.json` | `post_call_transcription`, `post_call_audio`, `call_initiation_failure` bodies |
| `live_get_conversation_opentelemetry.json` | Captured 2026-10-17 from the live API: user turn, three LLM turns with usage, one webhook tool call |

`live_webhook_post_call_transcription_otel.json` and `live_webhook_post_call_transcription_otel_timeout.json`
are real `post_call_transcription_otel` webhook bodies captured 2026-10-19 (conversation, agent, branch
and version ids and trace/span ids replaced; a test user id and agent name renamed). The first has a tool
call and `elevenlabs.user_id`; the second is a 2-minute call that ended on the time limit.
