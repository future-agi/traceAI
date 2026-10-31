# @traceai/auto

Zero-code AI tracing for Node.js 20.6+ (ESM and CommonJS). Two complementary
layers cover installed traceAI integrations and recognized AI HTTP wire protocols.
This is broad process-level coverage, **not a claim that every possible AI client,
transport, or framework is supported**. See the limitations below.

## Install and run

```sh
npm install @traceai/auto
export FI_API_KEY=your-api-key
export FI_SECRET_KEY=your-secret-key
node --import @traceai/auto/register app.mjs
```

Other entry points use the same singleton:

```sh
NODE_OPTIONS="--import @traceai/auto/register" node app.js
node -r @traceai/auto/register app.cjs
```

Or initialize before importing AI libraries:

```ts
import { init } from "@traceai/auto";
init();
```

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

### Relocatable launcher payload

The build also produces `dist/agent/`. Copy the **whole directory**, including
`runtime.cjs`, `hook.mjs`, `hook-register.cjs` and linked `.LEGAL.txt` notices:

```sh
node --require "/absolute/agent directory/preload.cjs" app.mjs
NODE_OPTIONS='--require "/absolute/agent directory/preload.cjs"' node app.cjs
```

Use `--require`, not `--import`, for launcher injection. No `@traceai/auto` install in
the application is needed. The payload embeds OpenAI instrumentation, including support
for OpenAI v4's default node-fetch transport; app-installed instrumentors take precedence.
The ES5-only preload skips Node <18 without loading/parsing the modern payload. Node
without `module.register` keeps CJS/fetch capture, not ESM library hooks. Historical
binaries were not exercised: the unsupported-version guard is VM-tested, and a missing
`module.register` is tested in a real child process. The supported package engine is
Node >=20.6.

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
detached snapshot; other exporters keep the original span/resource/context unchanged.
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
  `authorization_token` in MCP server settings.
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

### Library layer

`src/integrations.json` inventories all 45 instrumentation packages currently in the
TypeScript workspace, with their exported classes, target libraries and actual
module-hook entry points. OpenAI instrumentation is a dependency and is embedded in
the launcher payload; other dedicated packages are **not** installed implicitly.
Install the integrations you need, for example:

```sh
npm install @traceai/langchain
```

Target/instrumentation resolution is from the application's working directory.
Install packages in that app and run from its directory. Hook registration is eager;
target patching occurs on import/require. The import-in-the-middle loader is registered
before `registerInstrumentations`, restricted to target entry points rather than
all ESM internals (which can break SDK shim live bindings). Node 26 currently emits
a deprecation warning for upstream `module.register()` when library hooks are enabled.
Both ESM and CJS patching are tested against the real OpenAI npm package, with and
without app-installed instrumentation. OpenAI v4/node-fetch can additionally emit
Node's upstream `punycode` deprecation. Neither warning is suppressed by the agent.
Disabled integrations are never loaded or registered, including integrations sharing
a target (for example DeepSeek and OpenAI).

For Genkit, Mastra and Vercel AI SDK, the registry calls an installed `enable`
entry (also accepting the integration's named enable function). It prefers the
optional `/auto` subpath. Versions without that entry are reported `manual`, not
forced into an incompatible constructor; a broken entry fails open. The entry
receives `{ tracerProvider, spanProcessor, traceConfig }` for the selected agent
branch; older no-argument entries can ignore these optional arguments and use the
initialized agent. An instrumentor returned by the entry is explicitly bound to
that provider, not an unrelated global provider. This does not replace any global
provider or synthesize FI credentials.
Manual-only integrations remain Claude Agent SDK (`wrapQuery`), Codex SDK
(`wrapCodex`/`manuallyInstrument`), Pi Coding Agent (session wrapper), and VoltAgent
(processor). Their underlying fetch AI calls can still be traced.
Existing dedicated instrumentations retain their supported-version and method
coverage constraints; registration does not expand those APIs. OpenAI Agents uses
additive processor registration (`exclusiveProcessor:false`), preserving application
processors. Shutdown recognizes processor/class-registry ownership as well as
shimmer wrappers, and leaves application-owned patches intact.

If `@opentelemetry/instrumentation-http` is resolvable in the app, it is enabled for
incoming SERVER spans only. Outgoing non-AI HTTP calls are not traced by default:

```sh
npm install @opentelemetry/instrumentation-http
```

### Existing OTel SDKs

An existing global SDK provider is retained, with the FI processor added beside its
existing processors. Its sampler/context manager remain authoritative. OTel JS 2.x
has no public late-add-processor API, so this uses the live MultiSpanProcessor list;
the public 1.x API is also recognized. FI project resource attributes are enriched
only on the FI export copy. If a custom provider does not expose either supported
attachment mechanism, the agent warns and uses an isolated AI provider without
replacing the global provider. This is a degraded fallback, not a promise of
processor attachment to arbitrary OTel implementations.

## Server bundlers

Exported adapters inject the built preload into **Node server entries only**:

```ts
// esbuild: { platform: 'node', plugins: [traceAI()] }
import { traceAI } from '@traceai/auto/esbuild';
// Vite: plugins: [traceAI()] — production Node SSR only, not client/webworker SSR
import { traceAI } from '@traceai/auto/vite';
// Rollup: plugins: [traceAI({ server: true })] — explicit target required
import { traceAI } from '@traceai/auto/rollup';
// webpack 5: { target: 'node', plugins: [new TraceAIPlugin()] }
import { TraceAIPlugin } from '@traceai/auto/webpack';
```

These are separate config examples, not imports to combine in one file. Adapters are
idempotent, preserve shebangs, and leave browser outputs alone. Real esbuild, Rollup,
Vite and webpack builds are tested, including plugin-only builds using external OpenAI
v4's default node-fetch transport (not just global fetch). Symlinked esbuild entries
are matched by their canonical paths; aliases, installed package entries,
extensionless entries, directory indexes and custom extensions use esbuild's own
entry-point resolver before path canonicalization. esbuild split/dynamic chunks do not get extra
preloads. CJS entries require the preload before their code while retaining strict-mode
directives (webpack uses its source-aware insertion API). A missing/broken preload warns
and leaves the application running rather than turning a deployment mistake into a crash.

**ESM executable entries** get a tiny bootstrap at the original output path, which
initializes the agent and then dynamically imports a sibling `*.traceai-app.mjs` payload.
A static preload import alone is too late: Node has already linked the application's
static dependencies before executing it. Deploy the payload and its sourcemaps/chunks
alongside the bootstrap. The payload retains its code and mappings without an injected
line; CJS sourcemaps account for the inserted preload.

This ESM bootstrap is for executable server entries, not library/handler modules:
exported ESM entries are rejected rather than silently losing live exports. The executing
entry's `import.meta.url` names the payload, not the bootstrap; programs relying on an
exact entry filename/main-module identity must instead use `node --require` externally.
For Next handlers, prefer `instrumentation.ts` below. Runtime/chunk code still lives in
the same directory. Bundler-specific advanced packaging modes beyond the tested cases
are unverified.

The injected absolute path must exist at runtime: deploy `dist/agent/` to the same
path or pass `{ agentPath: '/deployed/agent/preload.cjs' }`. Do not omit sibling files.

Preloading cannot recover package import boundaries erased by bundling. Keep AI SDKs
external for dedicated library hooks; bundled global-fetch calls still use wire capture.
Do not use these adapters for browser/edge outputs.

### Next.js

Prefer Next's `instrumentation.ts` with a runtime guard (works independently of the
selected Next bundler). Keep `@traceai/auto` external via `serverExternalPackages`
where your Next version supports it:

```ts
export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    await import('@traceai/auto/register');
  }
}
```

`withTraceAI(config)` from `@traceai/auto/next` is an optional **webpack-only** fallback;
it skips client/edge configs and preserves an existing webpack callback. The callback
wrapper, generated real-webpack config, and register snippet are tested. An actual
Next server build/start and Turbopack lifecycle are **unverified**; the webpack wrapper
does not cover Turbopack. Never import the Node agent in an edge instrumentation path.

## Edge runtime

`@traceai/auto/edge` is an explicit fetch observer, not a zero-code Node agent:

```ts
import { patchFetch } from '@traceai/auto/edge';
const hook = patchFetch(4 * 1024 * 1024, (result, start, end) => {
  // Adapt to the isolate's own tracer/exporter, parent context and content policy.
});
// hook.remove() restores fetch. hook.pending contains completed-response decoding only.
```

Its ESM bundle has no Node APIs, Buffer, process/env access or OTel dependency. Isolated
VM tests prove JSON, rejection and streaming capture without those globals. It uses
the same catalog and privacy scrubbing. It does not install an exporter, read FI keys,
apply fi-core TraceConfig, propagate OTel context, or provide Node's suppression/dedup
layer: the adapter must supply those policies. Real Cloudflare/Vercel/Deno isolate
deployment is **unverified**. Native DecompressionStream availability is runtime-specific.

## Configuration

Precedence: code options > environment > config file > defaults. Code uses camelCase
(`projectName`, `wireCapture`, `sampleRate`, `instrumentations`, etc.); privacy flags
are under `traceConfig`. `exporter` is an optional SpanExporter override for embedding
or offline testing. It does not bypass the credential requirement.

| Environment | Default / meaning |
| --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Complete pair required for the default FI destination only; prefer environment, never commit keys |
| `FI_BASE_URL` | `https://api.futureagi.com`; FI collector base or full endpoint |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, else working-directory name, else `DEFAULT_PROJECT_NAME` |
| `FI_PROJECT_TYPE` | `observe`; `experiment` is opt-in |
| `FI_INSTRUMENTATIONS` | All; comma-separated allowlist, e.g. `openai,langchain,wire,http` |
| `FI_DISABLED_INSTRUMENTATIONS` | Empty; denylist wins over allowlist |
| `FI_WIRE_CAPTURE` | `true` |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304`; per request/response text buffering cap |
| `FI_SAMPLE_RATE` | `1`; clamped to [0,1], parent-based root sampling for agent-owned providers |
| `FI_AUTO_DEBUG` | `false`; print masked startup report |
| `FI_CONFIG_FILE` | `./futureagi.toml` |
| `OTEL_SDK_DISABLED` | `true` disables everything silently |

Example `futureagi.toml`:

```toml
project_name = "checkout-agent"
project_type = "observe"
instrumentations = ["openai", "langchain", "wire", "http"]
disabled_instrumentations = ["redis"]
wire_capture = true
wire_max_body_bytes = 4194304
sample_rate = 0.25
debug = false
hide_inputs = true
```

This is a flat subset, not full TOML: double-quoted strings, booleans, numbers and
single-line arrays of strings. Comments are supported, including `#` inside quoted
strings. Table declaration lines are ignored. Unknown/unsupported values are ignored.
File keys map to `FI_<UPPERCASE_KEY>`; `debug` maps to `FI_AUTO_DEBUG`.

## Doctor, bootstrap and statuses

```sh
traceai-auto doctor             # sends one explicit test export when enabled
traceai-auto doctor --no-check  # no test export; reports resolved config/status
traceai-auto bootstrap          # list coverage and suggested install commands only
traceai-auto bootstrap --install --package-manager=pnpm  # explicit opt-in installation
```

Bootstrap defaults to npm when `--package-manager` is omitted; npm/pnpm/yarn are
supported. It never installs target application libraries, only missing instrumentors
for detected targets. Restart the app after installing packages. The installation
branch is implemented but is not exercised against the public registry in tests.

Reports are JSON, with masked credentials and collector state (`accepted`, `failed`,
or `not_tested`). Startup never probes the collector. With zero sampling, doctor may
have no sampled test span to send; `not_tested` is intentional in that case.

- `instrumented`: wire hook, server hook, or a library module's patch callback ran.
- `pending`: hooks registered, but no compatible target patch observed yet.
- `not_installed`: no resolvable target library.
- `missing_instrumentor`: target is present but its dedicated package is absent.
- `disabled`: allowlist/denylist, credentials, capture flag, or SDK disable switch.
- `manual`: installed target needs explicit wrapping/callbacks; reason included.
- `failed`: load, registration, patch or known unsupported-version failure; reason included.

### SDK attribute limits and streaming timing

Wire spans and fi-core `FITracer` library spans share deterministic pre-admission
projection. Late library output/usage can displace earlier content at the host's
attribute limit. Identity, outcome, usage, provenance and degradation markers precede
content; `output.value` is reserved when content is present. At small host limits,
excess canonical metadata is retained in `traceai.attribute_projection.metadata` as
JSON. Unknown attributes (including prompt variables, error details and image URLs)
never enter that capsule, and export-time hiding rechecks capsule members by their
original keys. The SDK dropped count and `traceai.attribute_projection.dropped_count`
include projected-out keys. Host limits are not raised. Exact known-secret and media
sanitization precede SDK value truncation; the authoritative detached export boundary
still runs afterward. Application spans from other tracers are never rewritten.

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

- **2026-10-30 (neutral Mastra adapter; unresolved A11):** The currently installed
  `@traceai/mastra/auto` adapter still requires an FI credential pair before its
  zero-code attachment, even when the agent has an application-supplied export
  destination. The core supplies the selected provider/processor to enable entries,
  and neutral Genkit/Vercel work, but that Mastra adapter ignores these arguments.
  Credential-free Mastra zero-code attachment requires the adapter to remove its
  credential gate. This is a remaining support gap, not a reason to synthesize keys.
- **2026-10-30 (stream timing bounds):** Timing retains at most 4096 transport byte
  boundaries/events. The audio byte-count path retains at most 64 KiB of timing
  prefix; missing later event timings are not fabricated. Compressed custom fetch
  responses without runtime decoding cannot map compressed offsets onto decoded
  events. These cases set `traceai.wire.timing_incomplete`; first-chunk timing remains
  available. Response content capture limits and opaque/pipe limitations still apply.
- **2026-10-30 (very small attribute limits):** A host permitting fewer than three
  attributes cannot retain separate content, loss marker and metadata capsule. The
  limit is honored rather than bypassed. Use a practical host span-attribute limit
  (16 or more) when protected metadata and output must coexist.

Dated **2026-10-30**. These are coverage/environment limitations, not claims of
universal instrumentation support.

- **2026-10-30 (S6 recognition):** Media and credential recognition follow the shared
  `privacy.json` catalog, not semantic inference. Unknown, non-magic base64 outside
  its payload fields is not classified as media. Exact scalar data URIs own their
  complete value; URIs inside prose are delimiter-scanned. Catalog depth/node/text
  exhaustion fails closed, retaining span identity/timing rather than partial content.
  Use the `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and `FI_HIDE_ERROR_DETAILS` controls
  when application content requires stronger protection than structural recognition.
- **2026-10-30 (configuration):** Config/profile parsing supports flat scalar values
  and string arrays, plus named `[profiles.name]` sections in `.fi/config.toml` and
  `$XDG_CONFIG_HOME/fi/config.toml` (default `~/.config/fi/config.toml`). Multiline
  TOML and arbitrary TOML expressions are not supported. Credential pairs resolve
  atomically: explicit arguments, environment, `futureagi.toml`, fi project profile,
  then fi user profile; incomplete sources are ignored with a value-free warning
  naming the missing half. Ignored FI halves from every source still seed S5 and
  cannot appear in diagnostics. `FI_PROFILE`/the `profile` option selects the profile.
- **2026-10-30 (ROUND5 B2 scope; zero-code/one-line init):** Credentials that never cross an instrumented
  transport and have no credential marker cannot be inferred from free text. Opaque
  `Request`/stream bodies are never parsed or buffered; a body-only credential echoed
  as plain text can therefore remain unknown when it is neither an environment/FI
  value nor explicitly registered. This includes `authorization_token` inside a native
  `Request` or `ReadableStream` body; the original credential-bearing application 401
  remains unchanged. Those unparsed fields are outside ROUND5 B2's parsed-body scope.
  Materialized `URLSearchParams` and native FormData text entries are harvested
  before capture limits, including duplicate fields; opaque pre-encoded form strings
  are not form-decoded. Suppressed calls observe headers/URL only, never bodies.
  Application transformations (signature extraction, JSON encoded inside
  strings, numeric re-typing, base64 or splitting) are not a general secret-detection
  guarantee. Use `registerSecret` for additional exact values and the `FI_HIDE_INPUTS`,
  `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` and
  `FI_HIDE_ERROR_DETAILS` controls for sensitive content/error surfaces.
- **2026-10-30:** One-line/manual instrumentation with transport hooks disabled has
  only initialization FI/environment seeds and explicit registrations; credentials
  never observed by those hooks are not automatically known. The bounded LRU can
  evict old values after 1024 distinct credentials; re-register long-lived values
  when reused. The same hide controls above remain available.
- **2026-10-30:** Transport credential-discovery exhaustion (more than 1024 distinct
  values in one observation, excessive nesting/nodes/text or an already-materialized
  body over the 4 MiB discovery cap) fails closed process-wide: subsequent FI spans
  keep identity/timing but drop content on every surface. Application requests,
  responses and other exporters are unchanged. This latch lasts until process restart;
  it cannot safely be reset while unknown credentials could still be echoed. Ordinary
  credentials longer than 8192 characters are supported within the aggregate budgets.
- **2026-10-30:** Unnamed in-memory esbuild ESM output (`write:false` without
  `outfile`/`outdir`) is left byte-identical to the no-plugin build: there is no
  deployable sibling path for the ESM bootstrap. Use an output path or external
  `node --require` preload for instrumentation. Unnamed CJS/default outputs receive
  their preload in memory, with no filesystem output access. Custom JavaScript output
  extensions are supported for named CJS and ESM outputs.
- **2026-10-30 (zero-code/one-line init):** Cancelling an unused clone can finalize the shared capture before the original
  branch is consumed. Cancelling a released reader can likewise finalize capture
  even when the native cancellation promise rejects. Application bytes, promises
  and errors remain unchanged, but the one span can be incomplete, omit later output/
  usage and end too early. These two cases were independently reproduced even when
  later consumption returns the complete answer and usage. Branch-aware cancellation
  finalization is deferred.
- **2026-10-30 (zero-code/one-line init):** A provider failure carried inside an HTTP 200 stream (for example OpenAI Responses
  `response.failed`) can retain HTTP-derived OK status without the provider error
  description, even when `gen_ai.response.finish_reasons` records `failed`. HTTP
  success is not currently overridden by that in-stream provider failure.
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
- Real OpenAI calls, real Agents processor coexistence and real Cursor class ownership
  are validated here; registry completeness does not imply every one of the 45
  packages works with every version. Optional enable-entry registry tests validate
  the adapter contract, not each external framework deployment. Instrumentations
  depending on obsolete module paths may remain pending.

## Development and verification

From `typescript/`:

```sh
pnpm install --ignore-scripts
pnpm run build:deps
pnpm --filter @traceai/auto run build   # TS references build OpenAI and Anthropic CJS prerequisites
pnpm --filter @traceai/auto test
pnpm --filter @traceai/fi-core test --runInBand
```

The agent builder requires the resolved OpenAI instrumentor to appear in esbuild's
input graph and fails the build if it is missing; it cannot silently produce a
wire-only launcher after an `--ignore-scripts` install.

Tests load **all 4,985 shared S6 privacy fixtures**, **all 47 original wire golden
fixtures**, and the **16 A8 token-algebra fixtures** from the repository at test time,
including unavailable requests, encoded gzip responses and mid-stream failures; they
assert names/status descriptions/exact attribute subsets, absence/presence and
`not_contains` privacy expectations, and compare catalog bytes. Local mock-server e2e tests spawn real Node `--import`, `-r`, and
NODE_OPTIONS entry points, using a local OTLP collector rather than Future AGI or a
real AI provider. Other tests cover body caps, nesting/suppression, binary/BYOB/audio,
configuration precedence, privacy, global SDK coexistence and incoming transactions.
