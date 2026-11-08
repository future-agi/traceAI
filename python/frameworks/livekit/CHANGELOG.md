

## [0.2.0] - pending release

### Added
- `record_egress` creates a short recording metadata snapshot beneath an explicitly bound session span, including available id, status, room/conversation, nanosecond timestamps and credential-free file/stream locators. It never reads or uploads media bytes.
- `wrap_egress_client` provides opt-in, per-service capture of successful async start/stop/update responses without changing delegate arguments, return identity, errors or cancellation. SDK compatibility still requires validation against the deployed service/version; this is not a process-wide patch.
- Helper-authored recording metadata passes FI privacy masks before span creation, isolated from ambient FI attributes even when the supplied FITracer has a weaker policy. Only the selected parent context is propagated through creation and ending; the caller's ambient context is restored afterward, including on failure. An implicit conversation derives only from the fully masked room, then passes conversation masks; an explicit conversation remains independently gated. Helper-authored sensitive sources retain `lk.pii.*` names and their aliases are created only on the FI export copy. Exact recording span names remain classified across tracer scopes even if samplers or SDK limits remove the operation attribute, preventing room-to-conversation fallback. The helper's location projection drops query parameters, fragments, userinfo and stream-key paths.
- The egress example now binds an explicit session envelope and demonstrates bounded stop/close cleanup. Real SDK/native-session and minimum-FI compatibility verification is pending; no playback or Cloud-root-discovery support is claimed.
- FI export re-checks the recording helper's reserved source, alias and session keys by name, including reserved keys in span events and links, no matter which code wrote them; other application keys export as written. If a privacy mask raises, the withheld marker `__REDACTED__` is exported, never the raw value.
- The bundled HTTP and gRPC FI exporters map recording snapshots; existing one-argument conversion calls keep working.
- Recording spans are always ended, including when span creation fails partway; the caller's context is restored afterward.

The recording helper controls only the metadata it writes. Attributes, events or links that your own samplers, span processors or code add are exported as written; the recording policy does not filter them, except the helper's reserved keys, which export always re-checks.

## [0.1.3] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.2 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.

## [0.1.2] - 2026-10-21
### Fixed
- Spans that end last in a LiveKit job, the root `agent_session` span among them, are no longer lost (a job process on Linux
  exits without running `atexit`, so they used to stay queued). `enable_*` flushes the tracer provider when the job shuts
  down, and again when the process exits, for LiveKit's own job spans and for tracing set up at import time; each waits
  at most 3 seconds. A new `flush_on_job_shutdown()` sends the trace at job end when tracing is set up outside a job.
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
