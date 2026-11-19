## [0.1.1]

### Fixed
- Inline blobs (`inline_data`) are no longer copied, as base64, into `input.value` and `output.value`; they appear as a MIME type and size note.
- A generator or other one-shot iterable passed as `contents` is never read by tracing, so the call behaves exactly as it does uninstrumented.
- The embedding `title` is masked together with the embedded text under `hide_inputs`, `hide_input_messages` and `hide_input_text`.
- The proto-plus option fallback only retries on the keyword-signature `TypeError` and no longer switches permanently on unrelated errors.
- The `fi-instrumentation-otel` floor is raised to 1.3.0, the first release whose `hide_inputs` hides embedding text.

## [0.1.0]

### Feature
- Instrument the legacy `google-generativeai` SDK: `generate_content` (sync, async, streaming), chat sessions and `embed_content`.
