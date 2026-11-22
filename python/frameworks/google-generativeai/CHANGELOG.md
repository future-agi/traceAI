# Changelog

## [0.1.0] - pending release

### Feature
- Instrument the legacy `google-generativeai` SDK: `generate_content` (sync, async, streaming), chat sessions and `embed_content`.
- Inline blobs (`inline_data`) are recorded as a MIME type and size note, never copied (raw or base64) into `input.value` or `output.value`.
- A generator or other one-shot iterable passed as `contents` is never read by tracing, so the call behaves exactly as it does uninstrumented.
- The embedding `title` is masked together with the embedded text under `hide_inputs`, `hide_input_messages` and `hide_input_text`.
- Requires `fi-instrumentation-otel` 1.3.0 or later.
