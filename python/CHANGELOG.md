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