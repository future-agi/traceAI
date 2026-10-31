# Zero-code auto-instrumentation (Python)

Trace every AI call in a Python process without changing its code. Install one package, set
two keys, and start the program through the agent.

```bash
pip install fi-instrumentation-otel
export FI_API_KEY=... FI_SECRET_KEY=...

fi-instrument python app.py
```

The agent:

- enables every installed traceAI integration (65 libraries: model SDKs, agent frameworks,
  vector databases, voice frameworks, tools) when its library is first imported, so libraries
  the program never imports cost nothing;
- traces AI API calls at the HTTP layer (the **wire layer**) for any SDK or framework that
  speaks a known protocol, including SDKs that have no traceAI package yet: OpenAI-compatible
  APIs (OpenAI, Azure OpenAI, Groq, Together, Fireworks, DeepSeek, xAI, OpenRouter, Mistral,
  vLLM, LM Studio, any gateway), Anthropic, Gemini and Vertex AI, Cohere, Bedrock, Ollama,
  rerank APIs, and speech APIs (OpenAI transcription and TTS, Deepgram, ElevenLabs,
  AssemblyAI, Cartesia);
- makes incoming web requests (FastAPI, Flask, Django, Falcon, Tornado) root spans when their
  OpenTelemetry instrumentation is installed, so each AI call sits under the request that
  caused it;
- defaults to an `observe` project, leaves the program's signal handlers alone, flushes at
  exit, and never stops the program: a missing key or a failing integration logs one warning.

## Three ways to start it, none of which touch your code

| Command | When to use it |
| --- | --- |
| `fi-instrument python app.py` | Any command: `fi-instrument uvicorn main:app`, `fi-instrument celery -A tasks worker`. Child Python processes are traced too. |
| `FI_AUTO_INSTRUMENT=true python app.py` | When you cannot change the start command (containers, PaaS, notebooks launched by a service). Every Python process in the environment with the variable set is traced. |
| `opentelemetry-instrument python app.py` | When the program already uses OpenTelemetry auto-instrumentation. Future AGI is added next to your exporters; disable it with `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=futureagi`. |

Or with one line of code, at the top of the program:

```python
import fi_instrumentation

fi_instrumentation.init()   # same behaviour as fi-instrument; returns the TracerProvider
```

`init()` takes the same settings as keyword arguments (`project_name=`, `instrumentations=`,
`sample_rate=`, ...) and any `register()` argument (`span_exporter=`, `batch=`, `transport=`, ...).
Explicit exporter headers override FI credentials from the environment, with either HTTP or
gRPC transport. Call `fi_instrumentation.auto.flush()` before a short-lived process (a Lambda
handler, a job) returns.

## Two layers, no duplicates

The **library layer** is the set of traceAI packages (`traceai-openai`, `traceai-langchain`,
...). They give the richest spans: agent steps, tool calls, retrievals, framework structure.

The **wire layer** is built into `fi-instrumentation-otel`. It hooks `httpx`, `httpx2`,
`requests` and `aiohttp`, recognizes AI protocols by their URL path and body, and records the
model, messages, tool calls, token usage, streaming output, time to first chunk and, for
audio, the transcript, MIME type, duration and byte counts. Raw audio is never recorded.
Streaming first-chunk and first-text milestones are separate: the latter means the first
non-empty text, reasoning, tool or generated-audio delta, associated with the arrival of the
chunk completing that event, not with deferred parsing time. This includes Responses
function-call names before argument deltas, Gemini generated inline audio, and Cohere v1/v2
text and tool deltas, including SSE event names when the payload omits its type. Cartesia
generated-audio chunks qualify too; metadata and playback/finish events do not.

Wire usage follows the shared A8 algebra: Anthropic/Bedrock input includes cache-read and
cache-write tokens; Gemini includes tool-use input and thought output. OpenAI cache/reasoning
counters are already included. Cumulative raw snapshots merge before normalization, preserving
null versus zero. Total is inclusive input plus output when both are known, with a differing
provider total retained separately as `gen_ai.usage.provider_total_tokens`. SSE frames contribute
usage/output only after their terminating blank line arrives: complete JSON in an unterminated
frame is not an observed snapshot, including on transport failure. Streamed JSON arrays retain
only complete observed member objects, even before the closing array bracket arrives; a partial
later member, including a split UTF-8 codepoint, cannot supply usage or hide earlier frames.
Their first-output timing uses the chunk completing the member, not the array's closing bracket.

When a library-layer model span (`LLM`, `EMBEDDING`, `RERANKER`, `GUARDRAIL`) is active,
a context marker keeps the wire layer out of the way, including through intermediate native
SDK spans. Framework spans (chains, agents, tools) do not stop it: the wire span becomes their
child, filling the gap when a framework traces its steps but not its model calls.

Some SDKs now emit their own OpenTelemetry spans. The Anthropic Python SDK, for example,
records an `anthropic.messages.create` client span with HTTP attributes but no prompts,
completions or token counts. The wire span is its child and carries the model call itself.

When the application already set up OpenTelemetry, the agent joins it instead of replacing it:
a provider created with `fi_instrumentation.register()` is reused as is, and any other SDK
provider keeps its exporters and also exports to Future AGI with the project resource.

## See what is traced

```bash
fi-instrument doctor
```

```
Configuration:
  api_key                    sk-a…                            (env FI_API_KEY)
  project_name               checkout-agent                   (file futureagi.toml)
  ...
traceAI integrations:
  anthropic          missing_instrumentor  pip install traceAI-anthropic; the wire layer still traces its anthropic.messages calls
  langchain          pending               enables when langchain_core is imported
  openai             pending               enables when openai is imported
  wire.httpx         pending               hooks httpx when it is imported

Collector: OK, https://api.futureagi.com/tracer/v1/traces accepted a test export (HTTP 200)
```

Statuses: `instrumented`, `pending` (enables on first import), `missing_instrumentor` (library
installed, traceAI package not; the wire layer still covers known protocols), `disabled`,
`manual` (needs a line of code, with the reason), `failed` (with the error), `not_installed`
(shown with `--all`). In code: `fi_instrumentation.auto.report()`.

`fi-instrument bootstrap` lists the traceAI packages for the model, framework, voice and
tool libraries installed in the environment; `fi-instrument bootstrap --install` installs them.
Add `--all` to include vector databases and web servers, which are often installed for other
reasons (boto3 for S3, redis for caching). `fi-instrument validate`
checks the configuration and the keys and exits non-zero on a problem, for CI and deploy checks.

## Configuration

Settings come from arguments to `init()`, then environment variables, then `futureagi.toml`
in the working directory (or the file named by `FI_CONFIG_FILE`). `fi-instrument init-config`
writes a commented template. Keep the keys in the environment. An explicit empty allowlist
(`instrumentations=[]` in code or the config file) enables no integrations; omitting the
allowlist enables the defaults. UTF-8 project names are preserved. FI project routing uses
`project_name`; it does not replace detected application resource identity (`service.name`,
`service.namespace`, `service.version` or other `OTEL_RESOURCE_ATTRIBUTES`). Owned providers
retain that identity, and a host-owned provider keeps its existing resource.

`FI_API_KEY` and `FI_SECRET_KEY` resolve as one atomic pair: explicit arguments, then the
environment, then the config file. An incomplete source is ignored entirely, with one warning
naming its missing half; it never shadows or combines with half of a lower-priority pair.
Explicit FI exporter headers follow the same rule, including HTTP and gRPC constructors.
`Authorization` headers still override environment FI authentication. When the `fi` CLI launches
the process, it supplies its resolved profile credentials through the environment; the agent
does not read the CLI's credential store directly.

```toml
project_name = "checkout-agent"
project_type = "observe"
disabled_instrumentations = ["requests"]
sample_rate = 0.25
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | none | Credentials. Without them the agent stays off. |
| `FI_BASE_URL` | `https://api.futureagi.com` | Collector. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, else the working directory's name, else `DEFAULT_PROJECT_NAME` | Project. |
| `FI_PROJECT_TYPE` | `observe` | `observe` or `experiment`. |
| `FI_INSTRUMENTATIONS` | all | Only enable these (names from `doctor`; `wire` is the wire layer). |
| `FI_DISABLED_INSTRUMENTATIONS` | none | Never enable these. `wire` turns the wire layer off; `requests`, `httpx`, `httpx2`, `aiohttp` turn off one client. |
| `FI_WIRE_CAPTURE` | `true` | Wire layer on or off. |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304` | Largest request or response body the wire layer parses. Larger bodies are recorded as truncated. |
| `FI_SAMPLE_RATE` | `1.0` | Fraction of traces kept (parent-based). |
| `FI_AUTO_DEBUG` | `false` | Print the integration report at startup. |
| `FI_AUTO_INSTRUMENT` | unset | `true` starts the agent in every Python process in the environment. |
| `OTEL_SDK_DISABLED` | `false` | `true` turns tracing off. |

All twelve `TraceConfig` privacy fields also work as flat TOML keys: `hide_inputs`,
`hide_outputs`, `hide_input_messages`, `hide_output_messages`, `hide_input_text`,
`hide_output_text`, `hide_input_images`, `hide_embedding_vectors`,
`hide_llm_invocation_parameters`, `base64_image_max_length`, `pii_redaction` and
`hide_error_details`. An explicit `config=TraceConfig(...)` wins over environment and file
settings. Otherwise each environment field wins over the corresponding file field; omitted
fields keep their defaults (content capture stays **on**). The resolved policy object drives
collection, export-time sanitization and `doctor` values/source diagnostics.

Every policy resolves through the same resolver (`fi_instrumentation._configuration`),
including the default `TraceConfig()` that instrumentors create when the application passes no
config, standalone `FITracer`, and the exporters' privacy boundary. Fields set in code win;
unset fields use the policy the agent pinned for the process once `init()` (or the zero-code
entry) resolved it, including a file named by `init(config_file=...)`. Without the agent they
resolve the environment, then `FI_CONFIG_FILE` / `./futureagi.toml`, then defaults. Booleans
accept `true/false`, `1/0`, `yes/no`, `on/off` (case-insensitive) on every path. Invalid
settings fail agent configuration (the agent stays off) rather than silently disabling a hide
flag. Manual paths never raise into the application: an invalid single value keeps its default
with one warning, and an unreadable file (bad syntax, or a missing `FI_CONFIG_FILE`) hides
inputs, outputs and invocation parameters, with one warning naming the file and line (never a
value), until it is fixed; explicit environment values still take precedence.

The `FI_HIDE_*` masking variables apply to wire-layer spans exactly as they do to the rest.
Speech transcripts follow their content side: `FI_HIDE_INPUTS` / `FI_HIDE_INPUT_TEXT` hide TTS
text; `FI_HIDE_OUTPUTS` / `FI_HIDE_OUTPUT_TEXT` hide STT text, including `gen_ai.audio.transcript`.
Message hide flags also cover raw JSON aliases, tool arguments and parameters. Input-image
hiding conservatively redacts raw request JSON, parameters and tool schemas that could retain
images, while keeping flattened plain-text message content.

### Bounded attribute projection

Spans follow the spec's A7 deterministic projection against the provider's actual attribute
count limit (`OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT`, `SpanLimits`, default 128; never raised).
Protected attributes are reserved first: degradation markers, identity (`gen_ai.span.kind`,
response ID, FI project routing), outcome (`error.type`, HTTP status, finish reasons),
provider/model/operation, every known `gen_ai.usage.*` counter and capture provenance. Bounded
content fills the remainder in atomic groups (a MIME companion never leaves its payload):
output messages, then `output.value`/output transcripts, then input messages newest first as a
contiguous suffix (the oldest are dropped first, and the aggregate `input.value` is omitted
when it would restore dropped messages), then tool definitions/parameters; optional metadata
comes last. When content is omitted, one reserved slot carries
`traceai.capture.dropped_content_count` (content keys not emitted; it accumulates). The same
candidates and limit always select the same attributes. Wire spans are projected before the
SDK admits any attribute; FI library spans are projected over the SDK container plus a finite
protected reservoir when they end. OpenTelemetry's own attribute/event/link drop counts also
include projection omissions and survive detached privacy snapshots, host-provider routing and
OTLP serialization.

### Privacy and stream safety

Every span forwarded through the agent's exporter passes through one sanitizer immediately
before serialization, after batching and framework mapping. This includes wire, library and
application spans. The same single implementation also guards every traceAI-owned route
without the agent: the exporter `register()` installs (its default FI exporter or a supplied
`span_exporter`), a directly constructed `TracerProvider()`'s default exporter, and
`HTTPSpanExporter` / `GRPCSpanExporter` under any processor (a batch is sanitized once, even when
routes nest). Processors the application adds to a `register()` provider are host-owned and see
the original span. The sanitizer creates detached snapshots, never edits the live/shared span,
and covers attributes (including arrays and non-finite numbers), status descriptions, event
names/attributes, links, resources, instrumentation scopes and tracestate. Global/native tracers
also receive privacy snapshots before forwarding to other processors. Unsupported values become
their type name without invoking user methods; nesting, node counts and text size are bounded.
On a sanitizer error, only identity, timing, kind and status code survive. Upstream scrubbing
is best-effort; the final export boundary is authoritative:

- one dependency-light core catalog reader (`fi_instrumentation._privacy_catalog`) provides
  the credential-name and structural-media predicates used by transport observation, registry
  harvesting, JSON walking and every export surface. The wheel ships byte-identical copies of
  `spec/auto-instrumentation/privacy.json` and all privacy goldens beside the wire catalog;
- recognized base64 magic (PNG/JPEG/GIF, WAV/WebP, MP3/ID3, Ogg/FLAC, MP4-family and PDF) is
  omitted under **any** key, regardless of size. Audio payload fields and MIME-declared payloads
  are omitted at any size, including numeric sample arrays and flattened/bracket-indexed paths.
  Unknown-format base64 in catalog media fields is omitted above 256 characters. Transcript,
  voice, format, usage and other audio metadata remain available. MIME siblings bind to their
  exact indexed parent, never another array element;
- exact `data:` values own their whole payload, including quotes, whitespace and embedded URLs;
  only free text uses delimiter-limited scanning. Base64 media becomes
  `<omitted: N base64 chars>`; other media/data URIs use an omission marker;
- credential-named fields at every depth become `<redacted>`. Name normalization, substring
  patterns, usage/identifier exclusions and credential ancestry come from the same catalog at
  every site. Token counts, token-detail objects and logprob token text are preserved;
- signed URL query credentials (AWS, Google, Azure SAS, API keys and tokens) are redacted;
- the process-wide known-secret registry replaces every registered value, longest first, in
  every exported string, across traces and threads, including later responses/errors and native
  SDK spans such as `anthropic.messages.create`. There is no per-trace grouping or inheritance.

The registry is seeded at initialization with FI keys (including explicit exporter-header
credentials) and credential-named environment variables, using the shared catalog patterns and
exclusions (`key`, `token`, `secret`, `auth`, `password`, `passwd`, `credential`, `signature`,
`sig`, `session`, `cookie`). It retains plain strings of
**eight or more characters**, up to **1024 values**; the least recently registered/observed value
is evicted. Export takes a lock-free immutable snapshot; values never appear in diagnostics.
Credential-named captured fields and signed URLs are structurally redacted regardless of length.

Transport observation is independent of creating a wire span. Headers and URL credentials are
harvested even for library-covered/suppressed and non-AI calls: HTTPX/HTTPX2 at final transport
dispatch after auth and request hooks, Requests at `HTTPAdapter.send` including DigestAuth and
redirect retries, and aiohttp at `ClientRequest.send` after `TraceConfig.on_request_start`.
Mutations by `on_request_headers_sent` are also observed before receiving the response. Requests
Digest challenges do not finalize the wire span at the intermediate 401. Suppressed calls never
buffer or parse request bodies; credential-named body fields are learned only when the wire core
already parses that body. Headers are also observed before exporting an HTTPX auth/hook exception.
Repeated HTTPX/HTTPX2 credential headers are observed as individual entries, including credentials
added by late request hooks, rather than their comma-joined mapping representation. Commas inside
an individual credential are preserved, not split. Bytes-valued Requests credential headers are
decoded with HTTP's lossless Latin-1 mapping for registration, including the complete Authorization
value and its scheme-free credential. Request headers retain their original values, multiplicity,
bytes and identity. Response bodies and application-visible values are never modified.

Integrations can register extra credentials they already have without maintaining their own
credential propagation:

```python
from fi_instrumentation import register_secret

register_secret(provider_credential)  # protects subsequent exports throughout this process
```

Values learned after a span ends but before its queued batch exports are still scrubbed at the
final boundary. Already exported spans cannot be retroactively changed.

Request streams are never read ahead or replayed. If a body is unavailable or over the body
limit, only catalog-provider hosts are captured, with `traceai.wire.body_unavailable` or
`traceai.wire.truncated` and no request content. Unknown-host routes still require a valid AI
request body. Multipart text fields are captured up to 64 KiB each; file bytes are counted,
never retained, and large audio uploads still produce spans. Multipart metadata is bounded.
Responses API requests require either `input` or `model`, including stored prompts.

Gzip/deflate request and response bodies are decoded within the body limit; Zstandard is decoded
when its optional Python module is installed. Encoded Brotli bodies are left undecoded: the
Python Brotli/brotlicffi APIs do not provide a hard expanded-output bound, so even a tiny
compressed chunk could otherwise allocate an unbounded buffer. Brotli bodies already decoded
by the application's client are captured normally. Unsupported or unbounded decoders set
`traceai.wire.undecoded_encoding` instead of trying to parse compressed bytes. A mid-stream
transport failure produces an `ERROR` span with the partial output and `error.type`; an early
consumer close or an exception raised by the consumer is `OK` with partial output and
`traceai.wire.incomplete=true`. Bodies read to EOF or exactly their `Content-Length` remain
complete even when closed without another read. Streaming endpoint variants are marked as
streaming regardless of framing (including Gemini JSON arrays). Cohere v1/v2 streams and
Ollama tool-only responses retain tools and usage. Text parts within a message use newline
separators; deltas of each streamed text block concatenate without separators.

### Initialization and lifecycle

`init()` and zero-code bootstrap configure the agent **once per process**. Subsequent calls
return the original provider and log one informational line, ignoring new arguments, environment
changes and config files. Set privacy, sampling, credentials, batching and project before the
first call. `auto_instrument()` can add integrations/transports later, but cannot remove,
rebind or reconfigure integrations already pending or enabled. An empty allowlist means
"enable nothing new", not "disable existing instrumentation". Explicit `settings` merge with
individual overrides without discarding exclusions or body limits.

On application-owned providers, FI sampling applies independently to agent-created wire and
library spans, even with an application sampler of `ALWAYS_OFF`. Supplying both
`tracer_provider=` and `span_exporter=` adds the FI route to that provider; its sampler and
existing exporters remain application-owned. FI-only spans go only to FI. Global-native
integrations retain the application's sampling policy. Already-dropped application spans cannot
be recovered. Agno/BeeAI and other live application singleton instrumentors are adopted without
resetting their handles or claiming their cleanup. LiveKit/Pipecat mappers operate on the actual
FI route, before the export sanitizer, for both simple and batch processors.

Text-only hiding preserves known message roles, tool-call IDs and function names while hiding
content/raw aliases (including `llm.input_messages.*`, `llm.output_messages.*`, system
instructions and tool arguments/results). Invocation hiding removes the aggregate and scalar
`gen_ai.request.*` aliases and `llm.invocation_parameters`. Raw requests are redacted because
arbitrary provider-specific parameters cannot safely be separated from content. Resource metadata
also receives PII masking.

HTTPX keeps the original send arguments, response objects/subclasses, helpers and native eager
read/cleanup behavior. Streams are not read until the application reads them. Requests' decoded
iteration and `response.raw` reads are observed without duplicate bytes; redirected eager reads
retain partial output/headers on failure. aiohttp callbacks see their original response object;
a callback exception is a consumer abort, not a transport failure. Relative URLs resolve through
the session's `base_url`. Start timestamps are captured before agent validation/copying; end
timestamps capture the last observed body byte/error before parsing, not the later span emission.

The application retains ownership of its provider and processors. `flush(timeout_millis=...)`
and `shutdown(timeout_millis=...)` bound caller waiting even when an exporter/teardown ignores
its timeout or a state lock is busy. Shutdown closes only agent-owned handles/routes, makes
pending hooks inert and removes wire patches without peeling off application wrappers. The
OTel `FutureAGIInstrumentor.uninstrument()` entry does the same. Shutdown flushes queued batches
and is terminal for the process; restart the process to initialize again. Cleanup queues behind
unfinished flushes, and lifecycle locks reset after fork. Normal-exit and chained SIGTERM
flushing wait at most five seconds, preserving the previous handler/default exit behavior.

## Integrations that need a line of code

A few integrations cannot be switched on from outside the program. `doctor` reports them as
`manual` with the line to add:

- **AG2** attaches its telemetry per agent: `traceai_ag2.setup(agent)`.
- **mcp-use** reports through a callback: pass `traceai_mcp_use.FutureAGICallback()` to the agent.
- **Pipecat** emits spans only for a `PipelineTask(..., enable_tracing=True)`; the agent
  installs the attribute mapping, the flag is yours to set.
- **ElevenLabs Agents** run on ElevenLabs; forward their webhooks with
  `traceai-elevenlabs-agents serve`. Direct ElevenLabs TTS and STT calls are traced by the wire layer.

The DeepSeek, Fireworks, xAI, MiniMax and vLLM packages wrap the OpenAI SDK, which the
`openai` integration and the wire layer already trace, so they are off by default; add them to
`FI_INSTRUMENTATIONS` to use them instead.

## Known limitations

As of 2026-10-31 (OTHER-class, not ship blockers):

- Export-time privacy on manual routes applies the process policy (pinned by the agent, else
  environment > `futureagi.toml` > defaults). An explicit per-instrumentor `TraceConfig` can hide
  more, but cannot reveal content that the process policy hides at the export boundary
  (S1/A1; 2026-10-31).
- `fi_instrumentation.BatchSpanProcessor` / `SimpleSpanProcessor` constructed directly by the
  application with its own non-FI exporter keep that exporter verbatim (an application-owned
  destination, S8); put it behind `register(span_exporter=...)` or use the FI HTTP/gRPC exporters,
  which sanitize themselves under any processor. gRPC sanitization is covered by a unit test that
  is skipped where the optional gRPC exporter is not installed (S1; 2026-10-31).
- Manual paths treat an unreadable config file as hiding inputs, outputs and invocation
  parameters, even when the malformed line is a key that an environment variable overrides; the
  agent path ignores such shadowed lines. An invalid single privacy value (for example
  `hide_inputs = 5`) keeps its default with one warning on manual paths, as `FI_HIDE_*` always
  has; the agent refuses to start instead (A1; 2026-10-31).
- FI library-layer spans whose writes exceed the host attribute limit before the span ends lose
  OpenTelemetry's oldest-inserted SDK attributes inside the SDK container (protected semantics
  are kept aside and survive). OTel's `dropped_attributes_count` reports those evictions;
  `traceai.capture.dropped_content_count` counts only keys omitted by the A7 projection. Wire
  spans are projected before admission and do not have this gap. The earlier
  `traceai.attributes.truncated` / `traceai.attributes.dropped_count` markers are replaced by the
  spec's `traceai.capture.dropped_content_count` (A7; 2026-10-31). Eviction and projection never
  separate media from its context for the export sanitizer: traceAI-owned spans record the media
  context of MIME/type keys the SDK evicts or the projection omits (bounded) and the projection
  keeps a media MIME/type companion in one atomic group with its payload, so the payload is still
  omitted even when its MIME key was evicted (B2/A7; 2026-10-31).
- Media context is recorded from span start on traceAI-owned routes (FITracer spans,
  `register()` and `fi_instrumentation` processors, and providers the agent or `init()` protects).
  Some keys are dropped before any traceAI hook can record them: constructor-supplied span or
  link attributes beyond the host limit (the SDK admits them before `on_start`), event and
  `add_link` attribute maps larger than their own SDK limit, and evictions on SDK-native spans
  that reach the FI `HTTPSpanExporter`/`GRPCSpanExporter` through a host-owned OpenTelemetry
  processor. The media context of such a container is unknown, so the sanitizer fails closed
  for that container only: every catalog payload field (for example `input.value`,
  `output.value`, message `content`, `data`, `body`) is exported as an omission marker, while span
  identity, timing, usage, metadata, transcripts and OpenTelemetry drop counts are kept. The
  application's own exporters still receive the original span. To keep text content, set
  attributes after the span starts, keep event/link maps within their limit, or put the exporter
  behind `register(span_exporter=...)` or a `fi_instrumentation` processor (B2/S1/A7; 2026-10-31).
  A host attribute value-length limit shorter than a media MIME value (under six characters for
  `audio/`, `image/` and `video/` types, under 24 for `application/octet-stream`) truncates it
  before any hook sees it; the payload is cut to the same length and can then be exported without
  media context (B2/S1; 2026-10-31).
- One span records at most 4,096 distinct MIME/type keys with a media value that were evicted or
  omitted. Past that bound, when a MIME value exceeds the sanitizer's text budget, or when the
  record cannot be read (for example a concurrent write racing an eviction on older OpenTelemetry
  SDKs such as 1.39, which evict inside `__setitem__`), the span is exported fail-closed with identity and timing only, as when the
  sanitizer's own sibling-hint budget is exceeded (S1; 2026-10-31).
- Attribute limits smaller than the protected set cannot retain every field: no content is
  admitted and the family-ordered protected prefix that fits is kept; a zero limit keeps nothing
  (A7; 2026-10-31).
- A10 recognition mirrors the spec oracle exactly (fixtures 1–340). Realtime/WebSocket event
  names and snake_case Gemini `function_call` / `executable_code` parts are not HTTP wire forms
  and do not establish first-text. Transport-encoded bodies the client has not decoded, and
  streams beyond the 16,384-chunk timing bound, omit first-text and set
  `traceai.wire.incomplete` when output was parsed (A10; 2026-10-31).

- Unknown-format base64 of 256 characters or fewer, without recognized media magic, audio
  payload ancestry or MIME siblings, cannot be identified as raw media structurally. The S6
  catalog deliberately preserves it rather than treating ordinary short strings as media.
  Use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and the corresponding `FI_HIDE_*_TEXT` controls for
  sensitive unknown-format content (S6; 2026-10-30).
- Application-transformed secrets (signatures extracted into plain text, JSON encoded inside
  another string, numeric re-typing, base64 encoding or splitting) are outside exact-value
  registry matching. Use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, the corresponding `FI_HIDE_*_TEXT`
  controls and `FI_HIDE_LLM_INVOCATION_PARAMETERS` for sensitive content (S5; 2026-10-30).
- Credentials that never cross an instrumented transport and have no credential marker are not
  automatically discovered. Register them with `fi_instrumentation.register_secret(value)` or
  enable the hide controls above. Values shorter than eight characters and values evicted from
  the 1024-entry LRU are not protected by exact-value matching; credential-named fields remain
  structurally redacted (S5; 2026-10-30).
- Manual instrumentors/one-line setup without the agent's transport hooks initially know only
  FI keys and initialization-time environment credentials (plus explicitly registered values).
  Unmarked echoes on those paths require `register_secret` or the hide controls above. Standard
  `fi_instrumentation.init()` enables transport hooks by default; explicitly disabling wire
  instrumentation disables their observation too (S5; 2026-10-30).

- Discovery uses filesystem/package paths without importing parents. Libraries available only
  through custom meta-path importers or some frozen/editable loaders may report `not_installed`
  and miss lazy library-layer activation. Normal installed packages and known wire protocols
  remain supported (round-three finding 11; round-four privacy verifier,
  OTHER finding 9; 2026-10-30; rechecked by the round-six verifier, 2026-10-31).
- Disable-after-enable, runtime reconfiguration, collector-credential rotation and restarting
  after `shutdown()` are unsupported by design. Configure once, use additive `auto_instrument()`
  to add integrations, and restart the process to change the export/privacy policy. Newly
  observed provider credentials can still enter the S5 registry without reconfiguration.
- Call `init()` before additive `auto_instrument()`. The reverse order can retain the original
  instrumentation provider while `init()` attaches its export route to another provider, losing
  FI wire spans. That unusual API ordering is not supported (round-four privacy verifier,
  OTHER finding 6; 2026-10-30; rechecked by the round-six verifier, 2026-10-31).
- Do not race first initialization with shutdown. Shutdown can complete during configuration
  resolution before initialization installs its route, leaving that later route enabled;
  repeated shutdown is terminal and cannot revisit it (round-four privacy verifier,
  OTHER finding 7; 2026-10-30; rechecked by the round-six verifier, 2026-10-31).
- Give shutdown a positive timeout. `shutdown(0)` marks the agent stopped and closes every FI
  export route at once, so no later span exports through traceAI (fixed 2026-10-31), but it does
  not start asynchronous teardown: library patches stay installed until the process exits and
  later shutdown calls cannot retry (round-four privacy verifier, OTHER finding 8; 2026-10-30;
  rechecked by the round-six verifier, 2026-10-31).
- HTTPX/HTTPX2 Digest authentication (sync and async) can finalize wire capture when the client
  consumes the intermediate HTTP 401 challenge, before its successful retry. The application
  still receives the original successful response and unchanged history; the wire span can
  instead report HTTP 401/ERROR with missing final output. Final-dispatch credentials are still
  observed for S5 privacy. Use a library-layer instrumentor for final-call telemetry on this rare
  authentication variant (round-five verifier, OTHER finding 7; 2026-10-30; rechecked by the
  round-six verifier, 2026-10-31).
- Native adapters that ignore the supplied tracer provider retain application sampling decisions;
  their already-dropped spans cannot be recovered. Missing optional instrumentor packages are
  reported, not installed automatically at startup.
- Encoded Brotli cannot be decoded under a hard expanded-output bound with the available Python
  APIs; it retains request/status metadata and reports `traceai.wire.undecoded_encoding`.
  Client-decoded Brotli works normally. Bedrock binary event-stream responses retain request/status
  metadata rather than decoded response content.
- First-text arrival evidence is retained for at most 16,384 buffered chunks and framed
  SSE/NDJSON events or complete JSON objects/array members. Exhaustion sets
  `traceai.wire.timing_truncated`; a later first-text timestamp stays unknown rather than being
  invented. First-text is unavailable for transport-encoded compressed bodies unless the HTTP
  client already decoded them; first-chunk timing remains available (A10; 2026-10-30).
- Response timing reflects bytes observed through the application's reads, not a packet-level
  timestamp. Unread responses are left unread; closing early records an incomplete span. Custom
  transport/response subclasses that bypass the standard HTTP client hook methods may require
  a dedicated instrumentor.
- Sanitization intentionally omits excessively nested/large values (one shared 4 MiB-character
  and 4096-node budget per snapshot), and replaces unsupported objects by their type name.
  Malformed or incomplete strings starting with `{`/`[` are omitted rather than risk exporting
  unparseable nested credentials/media. It does not call custom serializers, getters or `__str__`.

## Other languages

TypeScript, Go, C# and Java implement the same contract, catalog and golden fixtures: see
[`spec/auto-instrumentation`](../../spec/auto-instrumentation/README.md).
