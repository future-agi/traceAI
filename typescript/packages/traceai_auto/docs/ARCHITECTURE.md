# Capture architecture

[Quick start](../README.md) · [Configuration](CONFIGURATION.md) · [Framework recipes](FRAMEWORKS.md)

This reference describes the Node agent’s capture, privacy and lifecycle boundaries. For setup, start with the quick start.

## Initialization and ownership

`init(options)` configures the process **once**. It retains the existing provider,
configuration and hooks on every later call, logging one diagnostic line. It returns
the **same tracer provider object**, or `undefined` when initialization is disabled or
failed. Diagnostics are returned by `report()`, not `init()`. Other exports are
`forceFlush(): Promise<void>`, `shutdown(): Promise<void>`, and
`registerSecret(value: unknown): void` (also exported additively by `@traceai/fi-core`).
Integrations can register credentials observed outside a transport without supplying
span context or retaining per-trace credential groups. Non-string values and strings
shorter than eight characters are ignored; registration never coerces user objects.
Shutdown removes this agent's hooks/processors but never shuts down an
application-owned global OTel provider. Reinitialization after shutdown or failed
startup, changing privacy/exporter settings, and disabling an enabled integration
by calling `init()` again are unsupported: restart the process to change configuration.
For short-lived programs, finish reading responses, then `await forceFlush()` or
`await shutdown()`.

The preload does not perform network I/O. The first real completed span initiates
a bounded batch export, or `doctor` can explicitly send a test span. FI credentials
are required only for the default FI destination. `init({ exporter })`,
`init({ spanProcessor })`, `init({ provider })`, or an existing application OTel provider
activates instrumentation without FI credentials and without creating an FI export route.
With neither a destination nor a complete FI pair, both layers are disabled with one warning.
FI project routing never replaces application `service.name` or environment resource
identity. Export/integration failures warn once per
failure category, do not log credentials/provider bodies, and leave the app running.
A one-shot `beforeExit` handler flushes completed spans. The agent adds no SIGINT
handler. Its SIGTERM listener starts a bounded flush and preserves app-owned listeners;
without one, it restores the default action and re-raises SIGTERM (preserving the signal
exit). Explicit flush/shutdown have a five-second deadline. Shutdown reserves up to
2.5 seconds for flush and then invokes exporter shutdown with the remaining 2.5 seconds,
even if flush never resolves. A failed initialization rolls back this agent's hooks,
processor and newly installed globals; app-owned globals are retained. The first
configuration remains terminal, so a later `init()` cannot retry or rewire the
process. SIGKILL/forced termination cannot guarantee delivery.

## Capture layers

### Wire layer

The runtime reads a byte-identical packaged copy of the shared
`spec/auto-instrumentation/wire-protocols.json` catalog. It patches `globalThis.fetch`,
preserving the fetch function's name, length and custom properties via a Proxy.

Recognized formats include:

- OpenAI-compatible chat/completions, Responses, embeddings, image generation,
  moderation, transcription/translation and speech generation; compatible gateways
  and providers are recognized by the catalog. Unknown hosts use the endpoint's
  protocol-specific fallback (`openai_compatible`, `anthropic_compatible`,
  `gemini_compatible`, or `cohere_compatible`).
- Anthropic messages, Gemini/Vertex generate-content and embeddings, Cohere chat,
  embed and rerank, Bedrock converse/invoke, Ollama chat/generate/embed.
- Deepgram listen/speak, ElevenLabs TTS/STT, AssemblyAI transcript submission and
  completed polling, and Cartesia bytes/SSE TTS.

Request-body validation prevents unrelated application endpoints from being claimed.
Request streams, opaque Request objects and over-limit bodies are captured **only for
catalog providers**, without request content and with `traceai.wire.body_unavailable`
or `traceai.wire.truncated`. Unknown proxies need a validated materialized request.
The actual fetch is invoked before inspecting materialized bodies: no request cloning,
stream read-ahead or request-side wait is added.

Wire spans are OTel CLIENT spans with the saved parent context. Start time is the
transport handoff, before URL validation or request inspection; end time is the last
observed byte/error, captured **before** copying, decoding or parsing. Parsing is
excluded from duration. Streams are observed by tapping the existing readers and
iterators, not by replacing the body. The caller receives the **original fetch
Promise and original Response**, including subclass/helper methods and native headers,
URL/type, clone, tee, BYOB support (or its original rejection), cancellation promises,
and chunk identities. Capture copies bytes into its own bounded buffers without
transferring/detaching app storage. No read, reader lock, synthetic Response, automatic
clone or eager tee is created for an unread body. Export is never awaited between
chunks; reading multiple clone/tee branches produces at most one wire span.

Text/JSON/SSE/NDJSON capture stops at the configured byte cap, discards buffered
content, and sets `traceai.wire.truncated`. Audio/binary responses are counted, not
buffered. JSON/SSE TTS base64 audio is counted with a bounded streaming scanner,
without accumulating or decoding audio. If a custom fetch exposes compressed JSON/SSE
TTS bytes, the compressed capture is bounded by the body cap and decoded asynchronously
after consumption; decoded chunks go straight to that scanner, not a retained audio
buffer. The decoded-work cap also applies; exceeding it sets `traceai.wire.truncated`.
FormData text fields are read independently
of upload size, each capped at 64 KiB; file bytes are never read, and only their
size/MIME/filename are retained. Catalog embedding formats record dimensions, not
vectors. Use `traceConfig.hideEmbeddingVectors` (or `FI_HIDE_EMBEDDING_VECTORS=true`)
to also hide vectors in generic JSON fallbacks and forwarded library/application spans.

Native fetch normally decompresses responses. When a custom fetch exposes encoded
bytes, bounded asynchronous Node decoders handle gzip/deflate/Brotli before parsing,
plus zstd when Node exposes it. The Node-free layer uses Web `DecompressionStream`
for the codecs its runtime supports. Unavailable decoders
keep request-side metadata and set `traceai.wire.undecoded_encoding`. Already-decoded
JSON/SSE is not decoded twice. A transport failure mid-stream is ERROR with parsed
partial output; consumer cancellation is OK with `traceai.wire.incomplete = true`.

Wire **span creation** is skipped for OTel-suppressed contexts, nested captures, and
LLM/EMBEDDING/RERANKER/GUARDRAIL ancestors, even through intermediate native SDK
spans. Outgoing credential observation is never skipped by these rules: fetch reads
headers/URL without inspecting suppressed request bodies. Node HTTP and Undici
diagnostic-channel subscribers also observe outgoing headers/URLs (including direct,
cached clients and non-AI routes); they do not create additional wire spans. The FI processor keeps weak live ancestry, so a model kind set after span
creation is also respected. Both `gen_ai.span.kind` and older `fi.span.kind` are
understood, including FI-wrapped spans. Uncovered CHAIN/AGENT/TOOL spans remain
eligible parents. Session, user and other fi-core context attributes are propagated.

Wire attributes pass through fi-core's TraceConfig. Additional conservative handling
covers flattened message keys, audio transcripts, and raw body copies when content
hiding is enabled. `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`,
`FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`,
`FI_HIDE_INPUT_IMAGES`, `FI_HIDE_ERROR_DETAILS`, and `FI_PII_REDACTION` are supported.
TTS `gen_ai.audio.transcript` obeys input-hide flags; STT obeys output-hide flags.
Hidden transcripts use `[REDACTED]`, exactly like hidden input/output values.

### Privacy

One export-time boundary sanitizes **every span sent through the agent's exporter**:
wire spans, dedicated-library spans and forwarded application spans. It builds a
detached snapshot of the ended native span; other exporters keep the original
span/resource/context unchanged by that export policy. In auto destination mode,
foreign attributes are never rewritten by FI policy. FI-authored attributes still
receive write-time hide flags, image caps, PII and known-secret protection, so
other exporters never receive authored content that those protections hid.
Application active context uses the native span; instrumentor writes use a separate
FI wrapper. Explicit standalone `FITracer` retains its active-wrapper behavior.
This is an **interim export-built view**, not complete capture: data the host SDK
already dropped or truncated may be omitted or marked unknown, never reconstructed.
Attributes (arrays element-wise), status descriptions, event names/attributes,
link attributes/contexts, resources, instrumentation scope name/version/attributes
and tracestate values all pass through this boundary. Nonfinite numbers become
`"NaN"`, `"Infinity"` or `"-Infinity"`. Unsupported attribute objects become type
names; user attribute getters, `toString` and `toJSON` are never called. The standard
ReadableSpan context method is supported for library-generated snapshots.

Work is bounded across each snapshot (20,000 values, 8 MiB text, 32 array levels;
credential discovery also has depth/count limits). On a sanitizer error, content,
status descriptions, events, links, scope and resource content are dropped for that
span while its identity and timing survive. Upstream wire scrubbing is best-effort,
not the security boundary. Before TraceConfig masking, every recorded string is
scrubbed:

- Data URLs and long base64 media fields (`data`, `bytes`, `b64_json`, `file_data`,
  `audio_base64`, `images` elements) become `<omitted: N base64 chars>`. Image-generation
  `b64_json` uses the format contract's `<base64 omitted>` placeholder. Raw audio is
  never retained, even for tiny inline audio clips: scalar/array `audio` and
  `input_audio` JSON leaves, indexed flattened fields and `inlineData.data.0` with
  a sibling audio MIME type all use the same no-minimum-size audio policy.
  MIME metadata and transcripts remain subject to their own content-hide controls.
- Credential-named keys are replaced with `<redacted>` recursively, including
  `authorization_token` in MCP server settings. The only logprob-name exception
  is the explicitly bound provider-root policy below; access-key identifiers
  remain masked. Catalog credential shapes are checked independently of names.
- Signed URL parameters, including AWS/GCP signatures and Azure SAS fields, have their
  values replaced with `<redacted>`, including percent-encoded parameter names.
- A single `globalThis[Symbol.for('traceai.secrets')]` registry is shared by installed,
  embedded, CJS and ESM package copies in the process/isolate. It retains at most 1024
  values of eight or more characters, evicting the least recently registered/observed
  value. Immutable longest-first snapshots are cached until membership changes. At
  initialization it is seeded with configured FI keys and credential-like environment
  values (names containing KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL or AUTH).
  Transport-observed header/query values, parsed credential fields and explicit
  `registerSecret` calls augment it. Every exported span, including application-owned
  instrumentation spans and unrelated/later traces, uses the current registry at
  export time; there is no span-start credential inheritance or SDK-state inspection.
  Credential headers and export-boundary credential fields use one shared list,
  including `x-goog-api-key`, `anthropic-api-key` and `ocp-apim-subscription-key`.
  Values never appear in diagnostics, reports or error messages. Every registered
  value is removed from every exported string, including names, status, events,
  links, resource/scope attributes and tracestate; other exporters remain unchanged.
  Materialized bodies discarded by the capture cap, URLSearchParams values and
  uncropped/duplicate FormData text fields still undergo bounded credential discovery
  before disposal. File bytes are never inspected. Credential-discovery budget
  exhaustion or failure while inspecting a materialized body drops all exported
  content, not span identity, timing or status code; this also omits the truncation
  marker on that fail-closed snapshot. Opaque, unparsed bodies instead follow the
  scoped limitation below; their observable headers/URL still augment the registry.
  Configured FI keys remain protected by the export policy even after LRU churn.

For the closed, versioned `logprob_tokens` catalog paths, **logprob tokens are checked against registered secrets and credential shapes, not key-name rules**.
This exception is available only from the parsed OpenAI chat response root at the
instrumentation handoff: `choices[*].logprobs.content[*].token` and
`choices[*].logprobs.content[*].top_logprobs[*].token`. It does not apply to nested
JSON in user content, tool output, arbitrary attributes, or flattened key names.
If value masking changes any choice's `message.content` or `delta.content`, its
**entire logprobs block** is masked, including alternatives and byte arrays.
Buffered streaming responses are scanned by choice index before export; once a
choice is masked, earlier buffered blocks and all later blocks for that choice
are masked too. Concatenated deltas are checked for split secrets, and affected
delta text is masked. A masked individual token also loses its byte-array alias.
The staged OpenAI instrumentor exports the buffered response once, not each delta.
Other provider structures have no new exception without catalog/source evidence.

Hide policies match semantic families, including `input.*`, `output.*`,
`gen_ai.input.*`, `gen_ai.output.*`, system instructions, tools, image/file URIs and
side-specific audio transcripts. `FI_HIDE_LLM_INVOCATION_PARAMETERS=true` additionally
hides `gen_ai.request.parameters`, explicit invocation parameters and tool schemas.
`hideEmbeddingVectors` also covers JSON fallbacks such as generic Bedrock invoke,
not only spans classified as EMBEDDING. Image hiding conservatively hides raw input
and input-message families to prevent URI aliases escaping the image policy.

Do not put arbitrary secrets in prompts: unknown free-text secrets cannot always be
recognized. These policies cover dedicated-instrumentor spans only when they pass
through **this agent's** exporter; unrelated exporters retain their own policy.

## Library hooks and existing providers

Target/instrumentation resolution is from the application's working directory.
Install packages in that app and run from its directory. Hook registration is eager;
target patching occurs on import/require. The import-in-the-middle loader is registered
before `registerInstrumentations`, restricted to target entry points rather than
all ESM internals (which can break SDK shim live bindings). Node 26 currently emits
a deprecation warning for upstream `module.register()` when library hooks are enabled.
OpenAI v4/node-fetch can additionally emit
Node's upstream `punycode` deprecation. Neither warning is suppressed by the agent.
Disabled integrations are never loaded or registered, including integrations sharing
a target (for example DeepSeek and OpenAI).

Mastra is manual-only in this release. Use `createFIObservability` or `createFIMastraExporter`; the registry does not load a Mastra auto entry.

For Genkit and Vercel AI SDK, the registry calls an installed `enable`
entry (also accepting the integration's named enable function). It prefers the
optional `/auto` subpath. Versions without that entry are reported `manual`, not
forced into an incompatible constructor; a broken entry fails open. The entry
receives `{ tracerProvider, spanProcessor, traceConfig }` for the selected agent
branch; older no-argument entries can ignore these optional arguments and use the
initialized agent. An instrumentor returned by the entry is explicitly bound to
that provider, not an unrelated global provider. This does not replace any global
provider or synthesize FI credentials.
Manual-only integrations include Claude Agent SDK (`wrapQuery`), Codex SDK
(`wrapCodex`/`manuallyInstrument`), Pi Coding Agent (session wrapper), VoltAgent
(processor), ElevenLabs (client wrapper), Google GenAI SDK (`instrumentGoogleGenAI`)
and Hugging Face Transformers (module/pipeline wrapper). Their underlying fetch AI calls can still be traced.
Existing dedicated instrumentations retain their supported-version and method
coverage constraints; registration does not expand those APIs. OpenAI Agents uses
additive processor registration (`exclusiveProcessor:false`), preserving application
processors. Shutdown recognizes processor/class-registry ownership as well as
shimmer wrappers, and leaves application-owned patches intact.

If `@opentelemetry/instrumentation-http` is installed and resolvable in the app, it is enabled for incoming SERVER spans only. Outgoing non-AI HTTP spans are not enabled by default.

### Existing OTel SDKs

An existing global SDK provider is retained, with the FI processor added beside its
existing processors. Its sampler/context manager remain authoritative. OTel JS 2.x
has no public late-add-processor API, so this uses the live MultiSpanProcessor list;
the public 1.x API is also recognized. FI project resource attributes are enriched
only on the FI export copy. If a custom provider does not expose either supported
attachment mechanism, the agent warns and uses an isolated AI provider without
replacing the global provider. This is a degraded fallback, not a promise of
processor attachment to arbitrary OTel implementations.

## SDK attribute limits and streaming timing

In **auto destination mode**, host SDK count/value limits govern native admission.
FI does not clear native attribute storage, re-order it for FI projection, or replay
earlier attributes over later application writes. FI-authored values are protected
before admission; the FI snapshot is built only at export with a late-secret recheck.
Late output or usage dropped by the host is not recovered. Absent optional usage
details are omitted rather than converted to the string `"undefined"`.

Observed host-dropped attributes, events or links, strings at the configured value
limit (including exact equality), string arrays containing such a value, and
explicit `<truncated>` / `[truncated]` suffixes trigger lineage-wide withholding.
Every FI content surface becomes `<unknown: host-truncated>`; `session.id` stays
`<redacted>`, and `traceai.capture.host_data_unknown=true` marks every FI view.
Native SDK dropped counts are retained. Only a closed, typed non-content allowlist
is exempt from withholding, never from secret masking. The decision is sticky for
the native span and identity-retaining mapped/copied FI views, including later views.
There is no partial FI content view or content-derived prefix, length or digest.
Truncated MIME values never establish safe text. Values exported before a loss was
observed are not retracted; already returned snapshots are not mutated retroactively.

Explicit standalone `FITracer` retains deterministic pre-admission projection
(spec A7), including late library output/usage priority, atomic content groups,
`traceai.capture.dropped_content_count`, and the separate SDK dropped count.
The former `traceai.attribute_projection.metadata` capsule is not produced; older
capsules are still re-sanitized by original keys at export. Host limits are not
raised in either mode. The auto interim is not a guarantee of complete capture or
of retaining canonical output at small host limits.

`gen_ai.response.time_to_first_chunk` measures the first observed response bytes;
`gen_ai.response.time_to_first_text` separately measures the first nonempty
text/reasoning/tool/audio delta. `traceai.stream.event_arrival_seconds` records event
arrival offsets from request handoff, using byte-boundary timestamps rather than
parse completion time. Content detection includes Cohere tool chunks/reasoning,
nested Mistral content, Responses tool/code deltas, model-requested server/MCP tools
and Gemini executable code. Successful NDJSON EOF accepts a complete final JSON
record without a newline; interrupted or capture-truncated tails still require the
record delimiter, and partial JSON is never promoted to a completed frame.

## Known limitations

- In auto destination mode,
  a span whose attributes exceed the host attribute count limit exports the
  value-free `<unknown: host-truncated>` marker for FI content once the host drop
  is observed. FI does not project or reconstruct a partial content view. The same
  withholding applies to names, status, resource/scope, events and links throughout
  the identity-retaining lineage; `session.id` stays `<redacted>` and every FI view
  carries `traceai.capture.host_data_unknown=true`. A closed typed non-content
  allowlist retains only its validated values, still subject to secret masking.
  Values exported before a loss was observed are not retracted.
  Exact equality with an observed value limit is uncertain too. Native host spans
  and unrelated exporters are unchanged by this export-time policy. This is not
  complete capture; larger host limits reduce omissions but do not guarantee it.
  To keep FI content on spans with many attributes, raise the standard
  OpenTelemetry limit `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` (or `OTEL_ATTRIBUTE_COUNT_LIMIT`);
  the OpenTelemetry default is 128.
- Observed SDK loss now
  withholds all FI content, including ordinary text, base64, embedded/nested JSON,
  URL queries and error prose, with the generic unknown marker rather than a
  content-derived media-length marker. Malformed JSON tails still fail closed.
  Auto records native limit and value-free loss facts before mapping; copied spans
  retaining that lineage withhold all non-NON_CONTENT values rather than exporting
  a mapper-shortened prefix. Native spans and host exporters
  are unchanged by this export policy. Copies without source evidence consult global
  SDK limits before environment settings; an unrelated global provider can cause
  over-omission. Custom processors that erase identity, limits and loss evidence
  can still hide loss. Use `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` /
  `FI_HIDE_ERROR_DETAILS` for such routes; no lossless guarantee is made.
- Detected SDK
  truncation is now withheld before prose parsing. An upstream cut with no marker
  or observable SDK limit remains ambiguous: free text with no enclosing JSON,
  such as quoted `data`/`mimeType` after `upload failed:`, may lack the closing
  delimiter needed by structural recognition. Complete long `data` values retain
  catalog omission. Use `FI_HIDE_ERROR_DETAILS` for errors and `FI_HIDE_INPUTS` /
  `FI_HIDE_OUTPUTS` for captured input/output prose.
- Exact registered-secret
  matching does not recognize every unknown prefix. The FI view withholds detected
  host-truncated content before that check, including span/event/link detail.
  Native foreign spans intentionally keep their original bytes. Upstream/custom
  truncation that erases all loss evidence is still not guaranteed detectable;
  use FI hide controls and register complete credentials before authored writes.
- This tree retains the
  manual `@traceai/mastra` package. Use `createFIObservability` or
  `createFIMastraExporter`; the registry has no Mastra enable hook and does not
  resolve an `/auto` export. No zero-code Mastra support is provided by this registry.
- Timing retains at most 4096 transport byte
  boundaries/events. The audio byte-count path retains at most 64 KiB of timing
  prefix; missing later event timings are not fabricated. Compressed custom fetch
  responses without runtime decoding cannot map compressed offsets onto decoded
  events. These cases set `traceai.wire.timing_incomplete`; first-chunk timing remains
  available. Response content capture limits and opaque/pipe limitations still apply.
- The host
  SDK decides which native attributes it admits; FI does not reorder or reserve
  auto-mode slots for output, provenance or usage. Once any host loss is observed,
  The export boundary withholds FI content for the lineage even when some native content survived.
  A default limit such as 128 is not a promise that output survives a larger span.
  Explicit standalone `FITracer` projection is separate and remains supported.

- Media and credential recognition follow the shared
  `privacy.json` catalog, not semantic inference. With complete SDK context, unknown,
  non-magic base64 outside its payload fields is not classified as media. Exact scalar data URIs own their
  complete value; URIs inside prose are delimiter-scanned. Catalog depth/node/text
  exhaustion fails closed, retaining span identity/timing rather than partial content.
  Use the `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and `FI_HIDE_ERROR_DETAILS` controls
  when application content requires stronger protection than structural recognition.
- Config/profile parsing supports flat scalar values
  and string arrays, plus named `[profiles.name]` sections in `.fi/config.toml` and
  `$XDG_CONFIG_HOME/fi/config.toml` (default `~/.config/fi/config.toml`). Multiline
  TOML and arbitrary TOML expressions are not supported. Credential pairs resolve
  atomically: explicit arguments, environment, `futureagi.toml`, fi project profile,
  then fi user profile; incomplete sources are ignored with a value-free warning
  naming the missing half. Ignored FI halves from every source still seed the secret registry and
  cannot appear in diagnostics. `FI_PROFILE`/the `profile` option selects the profile.
- Credentials that never cross an instrumented
  transport and have no credential marker cannot be inferred from free text. Opaque
  `Request`/stream bodies are never parsed or buffered; a body-only credential echoed
  as plain text can therefore remain unknown when it is neither an environment/FI
  value nor explicitly registered. This includes `authorization_token` inside a native
  `Request` or `ReadableStream` body; the original credential-bearing application 401
  remains unchanged. Those unparsed fields are outside parsed-body credential discovery.
  Materialized `URLSearchParams` and native FormData text entries are harvested
  before capture limits, including duplicate fields; opaque pre-encoded form strings
  are not form-decoded. Suppressed calls observe headers/URL only, never bodies.
  Application transformations (signature extraction, JSON encoded inside
  strings, numeric re-typing, base64 or splitting) are not a general secret-detection
  guarantee. Use `registerSecret` for additional exact values and the `FI_HIDE_INPUTS`,
  `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` and
  `FI_HIDE_ERROR_DETAILS` controls for sensitive content/error surfaces.
- One-line/manual instrumentation with transport hooks disabled has
  only initialization FI/environment seeds and explicit registrations; credentials
  never observed by those hooks are not automatically known. The bounded LRU can
  evict old values after 1024 distinct credentials; re-register long-lived values
  when reused. The same hide controls above remain available.
- Transport credential-discovery exhaustion (more than 1024 distinct
  values in one observation, excessive nesting/nodes/text or an already-materialized
  body over the 4 MiB discovery cap) fails closed process-wide: subsequent FI spans
  keep identity/timing but drop content on every surface. Application requests,
  responses and other exporters are unchanged. This latch lasts until process restart;
  it cannot safely be reset while unknown credentials could still be echoed. Ordinary
  credentials longer than 8192 characters are supported within the aggregate budgets.
- Unnamed in-memory esbuild ESM output (`write:false` without
  `outfile`/`outdir`) is left byte-identical to the no-plugin build: there is no
  deployable sibling path for the ESM bootstrap. Use an output path or external
  `node --require` preload for instrumentation. Unnamed CJS/default outputs receive
  their preload in memory, with no filesystem output access. Custom JavaScript output
  extensions are supported for named CJS and ESM outputs.
- Cancelling an unused clone can finalize the shared capture before the original
  branch is consumed. Cancelling a released reader can likewise finalize capture
  even when the native cancellation promise rejects. Application bytes, promises
  and errors remain unchanged, but the one span can be incomplete, omit later output/
  usage and end too early. Later consumption can still return the complete answer and usage; branch-aware cancellation finalization is not provided.
- Captured OpenAI-compatible chat/completions and Responses JSON/SSE errors
  (including Responses `status: "failed"` and `response.failed`) override HTTP
  success in telemetry, retaining partial output and usage. Provider error codes
  and descriptions pass through the existing privacy scrubbing. Other provider-specific
  failure envelopes, or failures hidden by unavailable/truncated bodies, can still
  retain HTTP-derived status; application response bytes and errors remain unchanged.
- Native `pipeTo`/`pipeThrough` algorithms bypass instance reader methods. These
  paths retain metadata-only completion (`traceai.wire.body_unavailable=true`),
  without replacing the stream/sink or fabricating output bytes. Their completion
  timing is the pipe/downstream completion, not a byte-level transport timestamp.
  Directly borrowing prototype methods, or freezing/non-configurably replacing
  custom stream methods, can bypass observation entirely. Native Response helpers
  and ordinary readers/iterators/clone/tee have byte-level regression coverage.
- Custom lazy/non-native thenables are not a validated Node fetch implementation;
  the Node observer does not invoke their overridden `then` to start them. Node's
  observer uses V8 promise-parent notifications and native rejection mirrors to
  preserve unhandled/handled rejection events and default fatal exits for ordinary
  fetch/read promises. Pre-created custom promises consumed before agent startup
  are not fully observable. The Node-free edge adapter lacks that Node-specific
  rejection-lifecycle mechanism and is not a drop-in process agent.
- An SDK without a processor-removal API can retain one inert agent processor after
  shutdown. Agent callbacks stop, application processors remain active, and init does
  not add another processor. Optional enable entries own their integration's hook
  cleanup when they do not return a disable handle.
- Covered ancestry traversal is bounded to 256 wrapper/native ancestors; esbuild glob
  expansion is bounded to 10,000 filesystem entries and 64 recursive directory levels.

- No outgoing **wire spans** for `node:http`/`node:https`, direct `undici.fetch`/
  `undici.request`, custom fetch implementations captured before preload, WebSocket,
  native gRPC, worker processes, browsers or arbitrary non-fetch transports. HTTP/
  Undici diagnostics do harvest credentials independently of span creation. Preload
  each separate Node process and worker isolate (globals/registries are not shared
  across V8 isolates). OpenAI's included dedicated instrumentation covers its
  supported default transport; the global fetch wire hook does not cover node-fetch.
- URL matching is limited to the catalog. Restricted-provider endpoints on a custom
  proxy host may not match. No speculative interception of unknown AI protocols.
- Request objects and opaque/pre-encoded multipart streams are never cloned/read;
  known provider endpoints retain metadata/body-unavailable capture only. Use native
  FormData for text fields and file metadata. Binary request sizes are included only
  when the body or declared content length exposes the size.
- A response must be consumed or cancelled to finish capture. Unread or never-ending
  responses do not yield completed spans. Force-flush never waits for unread responses.
- Bedrock binary event-stream responses record request/status only. Audio streaming
  and base64 variants count bytes but do not reconstruct audio/timestamps. No audio
  bytes are exported.
- Registry completeness is not a guarantee that every instrumentor supports every upstream version. Instrumentations depending on obsolete module paths can remain pending.

## Redistributing the launcher

The built `dist/agent/` directory is relocatable. Keep the whole directory, including `runtime.cjs`, `hook.mjs`, `hook-register.cjs` and linked `.LEGAL.txt` notices. Use its `preload.cjs` with Node’s `--require` flag, not `--import`; no application installation of `@traceai/auto` is needed for this payload. OpenAI instrumentation is embedded; app-installed instrumentors take precedence.

The ES5-only preload skips Node below 18 without parsing the modern payload. A runtime without `module.register` has CJS/fetch capture, not ESM hooks. These compatibility guards do not change the supported package engine: Node 20.6+.

## Legal notices

The published package includes `dist/THIRD-PARTY-NOTICES`, with the full installed
LICENSE, LICENCE, COPYING and NOTICE files for third-party inputs to all four
launcher/edge bundles. `dist/third-party-manifest.json` maps each bundle to its
third-party package names and versions; first-party `@traceai/*` packages are listed
separately in the notices. These files supplement, and do not replace, the existing
linked `.LEGAL.txt` output. Keep them with any redistributed launcher or edge payload.
The build fails if a bundled third-party package has no readable, nonempty license
text file; a license identifier or linked source comment alone is not sufficient.

## Maintaining the coverage table

The README’s library table is generated from `src/integrations.json`. Run `node scripts/generate-readme.cjs` from the package directory after changing the registry; `--check` fails if the checked-in table has drifted. Runtime classes and supported methods remain owned by the individual integrations.
