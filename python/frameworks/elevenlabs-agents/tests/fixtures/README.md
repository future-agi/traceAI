# Fixtures

Built from the documented shape on
https://elevenlabs.io/docs/eleven-agents/customization/opentelemetry-traces (re-read
2026-10-15) and the attribute names the Langfuse cookbook reads from real exports
(https://langfuse.com/integrations/no-code/elevenlabs, `elevenlabs.tool.params`,
`elevenlabs.producing_llm`, `elevenlabs.llm_usage.<model>.<kind>`, `elevenlabs.turn.index`).

They are **not** captured from a live ElevenLabs workspace. Replace them with one
real body when one is available.

`{{MARKER}}` is replaced at load time with a unique string so tests can prove that
no content reaches an exported span when content capture is off. A few attributes
ElevenLabs does not send (`host.name`, `process.executable.path`, `project_name` on
the resource, a stack trace event) are adversarial extras for the privacy and
project-binding tests.

| File | What it is |
|---|---|
| `post_call_transcription_otel.json` | Webhook body: greeting, two user turns, two LLM turns, one tool call, one unknown span name, a signed media URL |
| `post_call_transcription_otel_error.json` | Abnormal disconnect: root status ERROR, failing tool with an exception event, interrupted agent turn |
| `post_call_transcription_otel_no_text.json` | Turns without text, zero-length root with `time_in_call_secs` |
| `get_conversation_opentelemetry.json` | `GET /v1/convai/conversations/{id}?format=opentelemetry` body |
| `other_webhook_types.json` | `post_call_transcription`, `post_call_audio`, `call_initiation_failure` bodies |
