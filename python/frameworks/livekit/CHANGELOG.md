## [0.1.2] - 2026-10-19
### Fixed
- `enable_http_attribute_mapping()` / `enable_grpc_attribute_mapping()` no longer drop the Future AGI exporter when LiveKit
  adds a span processor of its own (livekit-agents 1.8 adds a privacy filter), so traces are sent again. A processor added
  after `enable_*` now runs alongside the exporter instead of replacing it.
- The `lk.pii.*` attribute names LiveKit uses since livekit-agents 1.7 (for example `lk.pii.user_input` and `lk.pii.chat_ctx`)
  are mapped to `input.value` and `output.value` again, so spans no longer arrive without their content. The earlier names are
  still mapped.
- An LLM span whose function calls are empty (LiveKit sets `"[]"` on every LLM node) now has the reply alone as `output.value`,
  instead of `{"text": ..., "function_calls": "[]"}`. This applies to the earlier attribute names too.
- A failure while mapping attributes is now logged once as a warning instead of passing silently. The span is still exported,
  without the Future AGI mapping.
- The mapping is applied to a copy of each span; the span other exporters on the same provider receive (LiveKit Cloud's,
  for one) is left exactly as LiveKit made it, so LiveKit's redaction of `lk.pii.*` keys still covers everything it sends.
- An empty user input, reply, text to speak, tool argument or tool output no longer becomes the JSON text `""` in
  `input.value` or `output.value`.

### Changed
- The `fi-instrumentation-otel` floor is raised from 0.1.14 to 1.1.0. Releases before 1.0.0 lack the `GEN_AI_SPAN_KIND` and
  `GEN_AI_REQUEST_MODEL` attribute names the mapping needs, so it failed on every span; 1.0.0 fails inside `register()` with
  OpenTelemetry 1.45, which livekit-agents 1.8.7 and later require.

## [0.1.1] - 2026-03-10
### Feature
- Version bump for PyPI publish.

## [0.1.0]
### Feature
- Added support for LiveKit instrumentation.
