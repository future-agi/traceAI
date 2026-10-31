# traceAI Transformers

OpenTelemetry/FITracer instrumentation for **local** Hugging Face Transformers
inference. This is separate from `traceAI-huggingface`, which instruments the
hosted Inference API. Installing or enabling this instrumentor never downloads
models, selects a device, installs PyTorch, or starts a worker.

```sh
pip install traceAI-transformers
# Install the inference backend appropriate for your host separately.
```

```python
from traceai_transformers import TransformersInstrumentor

TransformersInstrumentor().instrument(tracer_provider=provider)
# Existing transformers.pipeline(...)(...) and model.generate(...) calls are traced.
# Optional: instrument(tokenizer=tokenizer) to decode direct model.generate calls.
# TransformersInstrumentor().uninstrument() restores owned hooks.
```

`provider` is an OpenTelemetry tracer provider configured with your exporter.
The auto-instrumentation registry is maintained separately; this package alone
does not enable the zero-code launcher. Repeated `instrument()` calls are
idempotent through OpenTelemetry's BaseInstrumentor. Suppressed contexts remain
untraced. All extraction and tracer failures fail open; return values, exception
objects, streamer objects and arguments are not replaced, retried or consumed.

## Captured operations

- Eager `text-generation`, `text2text-generation`, `summarization`, `translation`
  and chat-template pipelines: LLM spans, model name, bounded text-only input and
  output messages, supported generation parameters and usage. Chat continuations
  capture only the newly generated suffix, retaining the continued message's role;
  prior messages and prefilled text remain input even for non-assistant roles.
- `feature-extraction`: EMBEDDING spans, input text, input count, embedding
  dimension and tokenizer counts when available. **Vectors are never exported.**
- `automatic-speech-recognition`: LLM spans with operation `speech_to_text`,
  transcript and duration for in-memory mono arrays with a known sampling rate.
  **Audio bytes, arrays, filenames and audio URLs are never exported.**
- PyTorch `GenerationMixin.generate`: LLM spans with input/output token counts
  from returned IDs, including the attention mask for padded input. Optional
  tokenizer decoding supplies text for direct calls; pipeline calls use the
  pipeline's original text/chat messages. Nested generate calls enrich the
  pipeline span instead of creating duplicates.
- Hugging Face `TextStreamer`, `TextIteratorStreamer` and
  `AsyncTextIteratorStreamer`: first generated token latency, excluding the
  initial prompt `put`, recorded as `gen_ai.response.time_to_first_token` (seconds)
  and the existing traceAI `gen_ai.server.time_to_first_token` (milliseconds).
  Streamer methods/objects and application-managed threads remain unchanged.

Subject to the exporter's attribute budget (see Known limitations), spans carry
`gen_ai.span.kind`, `gen_ai.operation.name`,
`gen_ai.provider.name=huggingface`, `gen_ai.request.model`,
`gen_ai.response.model`, `traceai.capture.layer=library`, generation parameters,
usage and `gen_ai.response.latency` in seconds. Start/end timestamps bound the
original public inference call; instrumentation decoding/extraction is outside
that interval. Unlike a network transport span, a pipeline's own preprocessing
and postprocessing are part of the public call's duration. Error spans retain
only the exception type, never exception messages or stack traces.

Content goes through FITracer/TraceConfig masking and the core privacy helper
(best-effort upstream of the agent's authoritative export-time sanitizer),
including text-only hide flags, transcript aliases, credentials and signed URLs.
Extraction is bounded to 128 records, 65,536 text characters per aggregate and
65,536 token IDs. No arbitrary object is stringified or serialized.

## Tests

The unit suite uses lightweight local fakes, a real FITracer and an in-memory
OpenTelemetry exporter. The real integration suite uses CPU PyTorch, a tiny
random GPT-2 from `hf-internal-testing/tiny-random-gpt2`, and an isolated disk
cache. It also constructs a tiny random T5 locally to verify that a decoder-start
token prepended by Transformers cannot expose hidden decoder input as output;
only newly generated tokens contribute to output text and usage. Chat continuation
regressions cover assistant/user/system/tool roles, batched chats, all three input
hide flags and per-call/pipeline-default continuation settings. Mapping messages
(including `OrderedDict` and accepted `UserDict` positions) and string subclasses
are normalized for prefix attribution just like plain dict/string values. Real
GPT-2 tests verify final-user continuations against an uninstrumented baseline,
with no system text or user prefill exported as output under any input hide flag.
They also exercise failing tracers and real OpenTelemetry processors with direct
and pipeline generation, `TextStreamer` and `TextIteratorStreamer`: span creation
failure installs no generation/streamer state and leaves inference unchanged.
Additional real-model regressions cover pipeline configuration, embedding-only
generation usage, completed KV-cache lifetime and default attribute-budget safety.
A tiny locally constructed Wav2Vec2 CTC model checks ASR dictionary duration before
Transformers destructively preprocesses `array`/`raw` and `sampling_rate`. Import
compatibility tests emulate allowed older cores missing the new privacy API and
verify that no inference or streamer hooks are installed.
Set `TRACEAI_TRANSFORMERS_REAL_TEST=1` to opt into the real-model suite; GPT-2 must
already exist in the package's `.cache/hub` cache. Model/tokenizer loading is
strictly local-only, and tests are explicitly skipped without the opt-in flag.
It uses no hosted inference API, credentials, accelerator, Docker or external
collector. Unverifiable or oversized explicit decoder prompts omit output text
and output/total usage rather than risk misclassifying input as output.

## Known limitations

- **2026-10-30:** Older allowed core packages without
  `fi_instrumentation.auto.wire._privacy` (including the original
  `fi-instrumentation-otel` 1.2.1) remain import-compatible but disable this
  instrumentor with a warning, leaving all hooks and inference untouched. Use a
  core release providing the agent privacy API to enable tracing; there is no
  separate fallback sanitizer or secret registry.
- **2026-10-30:** Supported dependency range is Transformers `>=4.41,<6`; the
  real-model verification is on Transformers 4.57.1/PyTorch 2.8.0, CPU/macOS.
  Custom generation implementations that override `GenerationMixin.generate`,
  TensorFlow/Flax generation, remote-code models and non-listed tasks are not
  patched. `trust_remote_code` is never enabled by tests or instrumentation.
- **2026-10-30:** Lazy dataset/generator pipeline inputs are not consumed or
  wrapped. Non-ASR lazy invocations have no eager pipeline content span; their
  later `generate` calls can be traced. **Lazy ASR is an exception:** it currently
  emits an OK speech span when the iterator is returned, before inference, with
  no transcript or actual inference duration. A later iterator failure does not
  update that successful span. Do not use it to assess lazy ASR success or latency.
  Finite list batches are flattened into bounded message attributes
  (batch/message grouping is not retained).
- **2026-10-30:** Eager pipelines defer extraction of sequence IDs and retain
  their input tensors until the entire call finishes, for up to 128 generation
  batches. Returned KV caches, logits and attentions are not retained by the
  instrumentation; sequence/input tensor retention can still increase peak memory.
- **2026-10-30:** Generation-parameter attributes describe the selected effective
  generation configuration and explicit public overrides. Task-specific internal
  adjustments (for example prefix-dependent changes to `max_length`) are not
  reflected. The inference configuration itself is unchanged.
- **2026-10-30:** Decoder-only embedding prompts without IDs can report output
  usage from returned generated IDs; input and total usage still require an
  attention mask. Embedding tensors are never decoded or exported.
- **2026-10-30:** Embedding input cardinality and tokenizer usage are calculated
  from capture-truncated messages. Batches above the 128-record cap silently
  undercount inputs and tokens (129 two-token inputs report 128 inputs and 256
  rather than 258 tokens). Deferred generation usage is also capped at 128 model
  calls per pipeline invocation: 129 single-item calls generating one token each
  report 128 output tokens. Neither cap truncates inference or its results.
- **2026-10-30:** Flattened batch content is subject to the OpenTelemetry attribute
  budget. Identity, model, usage and timing metadata are refreshed after content
  so they survive ordinary batches at the default 128-attribute limit, but message
  fields can still be evicted. Provider limits smaller than the metadata set can
  lose metadata too; collector limits may impose additional truncation.
- **2026-10-30:** Feature-extraction tokenizer estimates re-encode captured text
  without effective `truncation`/`tokenize_kwargs` or pipeline preprocessing
  defaults. They can overcount tokens (for example 61 versus an actual truncated
  length of 8). These estimates never change inference or its results.
- **2026-10-30:** Manual instrumentation without the zero-code agent's transport
  hooks only knows FI keys and initialized credential-like environment values
  through the core registry. Unmarked credentials that never crossed an
  instrumented transport, and application-transformed secrets (for example split
  or encoded tokens), may remain in text. Use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`,
  `FI_HIDE_INPUT_TEXT` and `FI_HIDE_OUTPUT_TEXT` when that text must not be captured.
  Environment credentials added after initialization must be explicitly registered
  with `fi_instrumentation.register_secret(value)`; the environment is not scanned
  again on each call.
- **2026-10-30:** Direct generation does not normally own a tokenizer: it records
  counts, not decoded text, unless `instrument(tokenizer=...)` or the call's
  `tokenizer=` supplies one. ID arrays above the extraction cap omit counts and
  decoding. Without an attention mask, padded input counts include padding.
- **2026-10-30:** Output usage stops at the first EOS token, but per-row non-EOS
  stopping criteria can leave padding that is indistinguishable from generated
  tokens in the returned IDs. Such padding is counted as output (for example
  suffixes `[3,0,0]` and `[3,3,3]`, EOS 99 and pad 0, report 6 rather than 4).
  The returned sequences and stopping behavior are never changed.
- **2026-10-30:** Custom streamers overriding `TextStreamer.put` without calling
  it cannot report TTFT. Streaming refers to synchronous `generate` feeding a
  streamer, not a replacement async iterator. TTFT observes the token handed to
  the streamer (including a special token), not its first nonempty decoded text.
- **2026-10-30:** File/URL/encoded-audio duration is not guessed and audio is never
  opened or decoded by instrumentation. ASR batch duration and chunk timestamp
  metadata are not aggregated; transcripts are bounded. Embedding counts use
  tokenizer estimates; non-generating ASR models omit token usage.
- **2026-10-30:** Instrumentation extraction is best effort. With unsupported
  return formats a span can contain identity/timing only. Hooks installed by
  another instrumentor after ours are not overwritten by `uninstrument()`;
  embedded copies of our wrappers become permanently inert, including after
  re-enabling. These third-party wrappers can retain an inert wrapper layer.
