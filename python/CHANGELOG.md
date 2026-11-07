## [1.3.0] - pending release
### Added
- Semantic conventions for agent cost, time to first token and coding-agent spans in
  `fi_instrumentation.fi_types` (#605): `tool_call.id`, `tool.name`, `fi.usage.*`, `fi.cost.kind`
  and the `fi.agent.*` attributes.

### Changed
- **The base64 image cap now defaults to 0** (it was 32000). `base64_image_max_length` and
  `FI_BASE64_IMAGE_MAX_LENGTH` keep working: set a positive value to export image payloads up
  to that many characters.
- Default cap 0 redacts any value or token that mentions a data: image URL, including mentions with no payload.
- The replacement is `[image-redacted]`, for the whole value or JSON string token.
- At the default cap 0, a non-URL value in an image payload position (typed image attributes,
  and the payload fields of image content parts) is replaced with `[image-redacted]`, even when it
  is short or is already a `__REDACTED__` marker. URL references in image content parts, such as
  `https://...`, are kept.
- An invalid, negative or non-integer cap resolves to 0 and logs
  `Invalid base64 image cap; using 0.` instead of raising.
- The TypeScript SDK is stricter: it never exports base64 images at any cap.

### Known limitations
- `fi-instrumentation-otel` currently requires `wrapt>=1.15.0,<2.0.0`; environments that require `wrapt>=2` cannot satisfy this dependency.
- Headerless base64 images (PNG, JPEG, GIF, WebP without a data: URL) are redacted only when they are the whole value or a whole JSON string token; padded or embedded headerless runs are exported.
- A declared image payload whose base64 contains spaces is exported.
- An over-cap image payload stored under a key outside the payload fields (data, bytes, content, image, image_url, url, base64, b64_json) of an image-declaring object, without a data: URL, is not detected by the image cap (e.g. {"type":"image","mime_type":"image/heic","blob":"<base64>"}); other media policy may still withhold it.
- Residual with an explicit positive cap: a payload that becomes recognizable only after multi-pass decoding inside a JSON string token, after a precise redaction has made the scope strict JSON, can be exported. The default cap 0 is not affected.
- With an explicit positive cap, the precise recognizer keeps this residual.

### Notes
- `traceai-agno` needs `fi-instrumentation-otel>=1.3.0`; its dependency floor will be raised in the agno release that follows the core release.

## [1.2.2] - 2026-10-30
### Fixed
- Apply content privacy to creation attributes and links before OpenTelemetry SDK
  sampling, and to sampler-produced attributes before span processors run. Preserve
  the sampler's decision, trace state, attribute selection and ordinary retention
  limits; keep FI's priority only for a caller-declared span kind.
- Extend input/output hiding to known tool, image, retrieval, reranking, audio,
  voice and computer-use content fields and their descendants, including event,
  exception and link attributes. Keep non-content metadata available. Mixed-direction
  content is hidden when either input or output hiding is enabled. These controls
  cover known semantic keys, not arbitrary custom attributes or free-text secrets.
- `FI_HIDE_EMBEDDING_VECTORS` now also hides `gen_ai.embeddings.vectors` and its
  descendants without evaluating hidden lazy values.
- Restore nested and concurrent `suppress_tracing` contexts correctly, including
  asynchronous use and exception exits.
- Honor explicit positional and keyword HTTP/gRPC exporter endpoints and headers
  instead of overwriting them with environment defaults.

### Changed
- Provider initialization leaves application SIGINT and SIGTERM handlers untouched.
  Normal-exit cleanup is still registered; applications must call `provider.shutdown()`
  through their own shutdown lifecycle to export pending spans on SIGTERM or SIGINT.
  See [application-owned shutdown](README.md#application-owned-shutdown) for an example.

## [1.2.1] - 2026-10-26
### Fixed
- `register()` no longer marks its own exporter as a default that the next
  `add_span_processor()` call replaces. Before, adding any span processor to the
  provider `register()` returns (a console exporter, a second backend, or a
  framework's own processor, such as LiveKit's privacy filter) shut the Future AGI
  exporter down and removed it, so traces silently stopped reaching Future AGI.
  The same happened to a custom `register(span_exporter=...)` exporter.

### Changed
- `add_span_processor()` on the provider `register()` returns adds the processor
  next to `register()`'s exporter, as in upstream OpenTelemetry, instead of
  replacing it. Code that relied on it to swap out `register()`'s exporter now
  exports to both; pass `register(span_exporter=...)` to export somewhere else
  instead.
- `register(span_exporter=...)` followed by `add_span_processor()` keeps the custom
  exporter, which used to be dropped, so spans go to both.
- If you pointed a second OTLP exporter at Future AGI to work around the old
  behaviour (for example traceai-strands with `OTEL_EXPORTER_OTLP_*` set to the
  Future AGI endpoint), remove it, or every span arrives twice. traceai-strands
  0.1.1 does not add its exporter when it would send the same spans to the same
  traces URL with exactly the same header names and values as `register()`'s
  exporter; an extra or different header on either side keeps it.
- Not changed: a `TracerProvider()` constructed directly still replaces the default
  `SimpleSpanProcessor` added on construction on its first `add_span_processor()`
  call.

## [1.2.0] - 2026-10-18
### Changed
- **Behaviour change for users of the hide flags:** setting any of `hide_inputs`,
  `hide_outputs`, `hide_input_messages`, `hide_output_messages`, `hide_input_text` or
  `hide_output_text` (or their `FI_HIDE_*` env vars) now also hides error details.
  Error text is content: provider error bodies quote the prompt back and carry request
  and session ids, and tracebacks carry local paths. Failed spans keep status `ERROR`,
  `error.type` and `exception.type`, but the status has no description and exception
  events drop `exception.message` / `exception.stacktrace`. `error.message`,
  `exception.message`, `exception.stacktrace` and instrumentor keys ending in
  `.error.message` / `.error_message` are dropped. Set `hide_error_details=False`
  (or `FI_HIDE_ERROR_DETAILS=false`) to keep the old behaviour.
- An `ERROR` status description is now the exception message only. The
  `"<Type>: <message>"` form built by OpenTelemetry's `use_span` and most instrumentors
  becomes `<message>` (the type is in `error.type`), also when one such line is joined
  per exception (llama-index), and a Python traceback is never kept in the description.
  The cleanup uses plain string operations, so a multi-megabyte exception message adds
  about a millisecond to `set_status` (no regular expression is built from message text).

- **Content-hiding fix:** `hide_inputs` (`FI_HIDE_INPUTS`) now also redacts `raw.input`
  (the raw request body, messages included) and the prompt template variables
  (`llm.prompt_template.variables`, `gen_ai.prompt.template.variables`), and
  `hide_outputs` (`FI_HIDE_OUTPUTS`) also redacts `raw.output`. Before, an instrumentation
  that set these kept the prompt in the stored row while the hide flag was on. Same keys
  as `@traceai/fi-core` 1.1.0.

### Feature
- New `TraceConfig(hide_error_details=...)` / `FI_HIDE_ERROR_DETAILS`. Unset, it is
  derived from the content hide flags above; an explicit `true`/`false` wins.
  `dataclasses.replace(config, hide_inputs=True)` derives it again from the new flags
  unless it was set explicitly.
- Spans that end with status `ERROR` get `error.type` (an exception recorded on a span
  that still ends OK, such as a recovered retry, does not set it). It is the provider
  error code for exceptions from known provider SDKs: openai and groq
  `code`/`type`, anthropic `error.type`, botocore/boto3 `Error.Code`, google-genai
  `status`, google-api-core gRPC status, mistralai body `type`, and the HTTP status for
  httpx `HTTPStatusError`. Every other exception gets its fully qualified class name
  (for example `sqlalchemy.exc.OperationalError`, never its `.code`). An instrumentor's
  own `error.type` is not overwritten. A failed span whose status is not about a
  named exception (for example `set_status(ERROR, "guardrail rejected")` after a retried
  timeout, outside an `except` block) gets `_OTHER` (OpenTelemetry semantic conventions)
  instead of a stale recovered exception. litellm exceptions carry no `code`/`type`, so
  they get their class name (for example `litellm.exceptions.NotFoundError`).

### Fixed
- `with tracer.start_span(...) as span:` now yields the FI span wrapper instead of the
  raw SDK span, so attribute masking, span-kind ordering and the error handling above
  also apply inside that block.
- `fi_instrumentation.instrumentation.version.__version__` now matches the package
  version (it still said `0.1.19`).

## [1.1.0] - 2026-08-11
### Feature
- `register()` now accepts span-processor / exporter / provider tuning:
  `max_queue_size`, `schedule_delay_millis`, `max_export_batch_size`,
  `export_timeout_millis`, `timeout`, `sampler`, `span_limits`, `span_exporter`.
  Unset values fall back to the OTEL env vars / upstream defaults (no behavior change).
- `BatchSpanProcessor` forwards its documented tuning params to the OpenTelemetry
  base class (previously silently dropped, causing unavoidable span drops under load).
- Re-exported `Sampler`, `ParentBased`, `TraceIdRatioBased`, `SpanLimits`.
### Fixed
- `register(batch=False)` warns when batch-only tuning params are passed.
- `register(span_exporter=...)` no longer crashes with non-OTLP exporters.
- Corrected docstrings that advertised an unsupported `endpoint` argument.

## [0.1.7] - 2025-06-10
### Feature
- Added support for ai-evaluation
- Added support for new evals in Prototype

## [0.1.6] - 2025-06-03
### Feature
- Added Support for Custom Evaluations in Prototype

## [0.1.5] - 2025-05-23
### Feature
- Added support for FutureAGI's protect
- Handling for incomplete spans during termination

## [0.1.4] - 2025-05-08
### Feature
- Bug fixes
- Support for new evals in Prototype

## [0.1.3] - 2025-04-14
### Feature
- Validations for Prototype eval mapping
- Added Support for Audio Evaluations