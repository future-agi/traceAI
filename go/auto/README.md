# traceAI Go auto

HTTP-level AI tracing for Go, with bounded capture, OpenTelemetry spans and the
same wire catalog and golden fixtures as traceAI's other auto agents. This is
an alpha transport agent, not runtime bytecode instrumentation: Go requires one
import and a rebuild. Dedicated library middleware remains opt-in.

## Install and run

Requires Go 1.26 (the OpenTelemetry 1.47 dependency minimum). With a recent Go
installation, `GOTOOLCHAIN=auto` downloads the required toolchain.

```sh
go get github.com/future-agi/traceAI/go/auto@latest
export FI_API_KEY=... FI_SECRET_KEY=...
export FI_PROJECT_NAME=my-service
go run .
```

Until the first `go/auto/v*` release, install from the branch containing this
module rather than assuming an existing public release.

Add the blank import before building; no per-call tracing code is needed:

```go
import _ "github.com/future-agi/traceAI/go/auto"
```

Or use the named import and one-line explicit initialization:

```go
shutdown, err := auto.Init(ctx)
```

Initialization is fail-open, network-free, and never installs signal handlers.
Without FI credentials, instrumentation still activates when the application
supplies an SDK tracer provider (explicitly or as the existing global provider)
or a span processor. With none of these destinations, it warns once and leaves
the default transport and tracer provider unchanged. An invalid explicit configuration
returns an error; importing the package never propagates an error to the app.
Collector failures are bounded by the batch queue and exporter timeout, emit a
single warning, and drop failed batches rather than interrupting application
traffic. Errors and warnings do not contain credential values or request URLs.

**Go has no atexit.** Short-lived programs must flush explicitly. A blank import
cannot ensure delivery at exit: a program that returns from `main` right after
its last AI call can exit with that span still queued. When the agent activates
at package initialization with its own batching FI exporter, it prints one
startup line (`auto.ShutdownHint`) saying exactly this. For reliable delivery,
use a named import and call the exported `auto.Shutdown(ctx)` before `main`
returns (`auto.ForceFlush(ctx)` flushes without shutting down):

```go
package main

import (
    "context"
    "time"
    "github.com/future-agi/traceAI/go/auto"
)

func main() {
    defer func() {
        ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        _ = auto.Shutdown(ctx)
    }()
    // Your application; HTTP clients can use their normal defaults.
}
```

Default batching is 200 ms, nonblocking, with a 5-second export timeout. This
reduces, but does not eliminate, loss on abrupt exit. Always read responses to
EOF or close their bodies: that is when the wire span completes.

## API and OpenTelemetry coexistence

- `Init(ctx, ...Option) (func(context.Context) error, error)` configures the agent
  once per process. Later calls return the original shutdown function, ignore
  options without evaluating them, and log one line (once). They never replace
  the provider, processor, resource, sampling policy or transports. A blank import
  that cannot activate (missing credentials, disabled or invalid configuration)
  leaves the first successful explicit initialization available. Set environment
  variables/config before startup; code options cannot override an active
  blank-import agent. Disable-after-enable and restart-after-shutdown are unsupported.
- `Shutdown(ctx)` removes the agent's processor and restores its default
  transport, but does not shut down a provider owned by your application.
- `ForceFlush(ctx)` flushes queued spans without disabling capture. Flush and
  shutdown respect the caller's deadline, with a five-second upper bound, and
  guard reentrant callbacks.
- `Report()` returns the configuration with credentials masked, integration
  statuses, middleware suggestions and wire coverage.
- `Transport(base)` wraps a custom `http.RoundTripper`; when the agent is off it
  returns the base unchanged. Example:
  `client := &http.Client{Transport: auto.Transport(customTransport)}`.
- `SuppressInstrumentation(ctx)` prevents capture for an explicit context. Go
  OTel exposes no universal public instrumentation-suppression context key;
  this does not recognize another library's private suppression key.
- `ContextWithSessionID`, `ContextWithUserID`, and `ContextWithAttributes` carry
  context attributes to child wire spans. Other modules' context keys are
  private, so their session/user helpers do not automatically bridge to this
  package. OTel parent span context still works across modules.

Options include `WithCredentials`, `WithProject`, `WithSampleRate`,
`WithWireCapture`, `WithTraceConfig`, `WithTracerProvider`, `WithSpanProcessor`,
and `WithConfig` (a complete replacement, not a sparse merge). A custom span
processor replaces the agent's network exporter and does not need credentials.
An application-owned SDK provider is also sufficient without FI credentials:
no FI exporter is created, wire spans go to its existing processors, and
`ForceFlush` delegates to that provider. The host owns its privacy/export policy;
agent shutdown only removes observation hooks, never shuts down host processors.

If a global `*sdktrace.TracerProvider` exists at initialization, the agent adds
its processor; existing exporters and the application's sampling policy are
untouched. Future AGI project resource attributes are applied to copies sent to
its exporter, not to other exporters. The application's resource identity is
authoritative in that copy too (A6): every application resource attribute,
including `service.name`, is kept exactly, even when `OTEL_SERVICE_NAME`,
`WithConfig` or `FI_PROJECT_NAME` differ. FI routing is the separate
`project_name`/`project_*` attributes; other agent defaults only fill keys the
application did not set. An agent-owned provider uses normal OpenTelemetry
resource detection (`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, SDK identity,
`unknown_service:<executable>` fallback); the FI project or its working-directory
default is never used as `service.name`. If no SDK provider exists, the agent
creates a parent-based ratio-sampled provider. An installed third-party API
provider is not replaced; a private SDK provider exports wire spans with their
original parent contexts. Installing another global provider *after* this
package's successful init cannot move its processor; a second `Init` cannot
attach it to that new provider. Configure the provider before the first successful
initialization (or use `WithTracerProvider` on that first explicit call).

Do not initialize, shut down or replace `http.DefaultTransport` concurrently
with HTTP requests. Existing clients that cached a different concrete transport
need `Transport(base)`. Cached agent wrappers stop capturing after shutdown;
calling `Transport(cached)` then returns its uninstrumented base, not a new agent.

Wire attributes are projected before entering the provider's actual attribute
container, including smaller host limits. Identity, outcome, usage, provenance
and degradation fields precede content; messages and MIME companions are atomic.
That protected set is closed (exact keys plus numeric `gen_ai.usage.*` counters),
never a key prefix, and the same candidates always select the same attributes.
A catalog MIME/type hint (for example `z.record.mimeType=audio/L16`) is admitted
with every group holding a key under its parent path, so projection never keeps a
payload while dropping its media context. Context attributes are scrubbed as one
object (with the wire span's hint keys as siblings) before projection, so
headerless audio identified only by a sibling MIME type is omitted in every view,
including host-owned exporters.
Output wins over input, retained input is a contiguous newest suffix, and an
aggregate input cannot restore dropped history. `traceai.capture.dropped_content_count`
counts omitted content keys (accumulating any prior count), while the legacy
`traceai.wire.attributes_truncated` flag also covers omitted metadata. SDK dropped
counts are kept separately and never rewritten. If the protected set itself
exceeds capacity, only its deterministic prefix fits and no content is admitted.

## Configuration

Precedence is **code options > environment > config file > defaults**.
`FI_CONFIG_FILE` selects `./futureagi.toml` by default. The supported flat TOML
subset is one key/value per line: quoted strings, booleans, integers, floats,
and single-line arrays of strings. Comments respect quoted `#`; table headers
are ignored (there are no nested table semantics). Keep secrets in environment
variables, not committed config files.

```toml
project_name = "checkout-agent"
project_type = "observe"
instrumentations = ["wire", "openai", "anthropic"]
disabled_instrumentations = []
wire_capture = true
wire_max_body_bytes = 4194304
sample_rate = 0.25
debug = false
```

Keys map to `FI_<UPPER_CASE_KEY>`, except `debug` maps to `FI_AUTO_DEBUG`.
`FI_API_KEY` and `FI_SECRET_KEY` resolve atomically per source: explicit options,
then environment, then this config file. An incomplete source is ignored as a
whole with a value-free warning naming the missing half; keys are never mixed
across sources. This module does not directly read fi's profile store; a launcher
can supply a complete profile pair through the environment.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | none | Future AGI credentials; both required for export |
| `FI_BASE_URL` | `https://api.futureagi.com` | OTLP/HTTP base; exports to `/tracer/v1/traces` with `X-Api-Key` and `X-Secret-Key`, no compression |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, else working-directory name, else `DEFAULT_PROJECT_NAME` | Future AGI routing project (`project_name`); never assigns or replaces `service.name` |
| `FI_PROJECT_TYPE` | `observe` | `observe` or `experiment`; no experiment registration/startup request |
| `FI_PROJECT_VERSION_NAME`, `FI_PROJECT_VERSION_ID` | `DEFAULT_PROJECT_VERSION_NAME` / generated UUID | Project resource keys |
| `FI_SAMPLE_RATE` | `1` | Parent-based root ratio when the agent owns the SDK provider; existing SDK sampling wins |
| `FI_INSTRUMENTATIONS` | all | Comma-separated allowlist of `wire`, `openai`, `anthropic`, `genai`, `eino`, `a2a` |
| `FI_DISABLED_INSTRUMENTATIONS` | none | Comma-separated denylist; `wire` disables all transport capture |
| `FI_WIRE_CAPTURE` | `true` | Wire-layer switch |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304` | Per-direction text/JSON capture cap; over-limit spans survive with `traceai.wire.truncated=true` |
| `FI_AUTO_DEBUG` | `false` | Print masked local coverage/config report at startup |
| `FI_CONFIG_FILE` | `./futureagi.toml` | Flat config path |
| `OTEL_SDK_DISABLED` | `false` | Disables the agent |

Allow/deny lists report manual library integrations as disabled; they cannot
remove middleware you explicitly added yourself. Provider-specific allowlists
do not filter wire requests: `wire` is the integration that owns that layer.

Privacy switches match the other Go modules: `FI_HIDE_INPUTS`,
`FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`,
`FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`,
`FI_HIDE_LLM_INVOCATION_PARAMETERS`, and `FI_BASE64_IMAGE_MAX_LENGTH` (32000).
Booleans count as true only for `true`, case-insensitively. Inputs/outputs are
redacted with `__REDACTED__`; messages are dropped or text-redacted. This module
also redacts raw JSON under text-hide switches and audio transcripts when
content is hidden, so the same content cannot leak through a second attribute.
HTTP headers are never attributes. Credential-shaped JSON keys at any depth,
signed URL query values, and echoed request credentials/FI keys (at least eight
characters) are replaced with `<redacted>`, including in error descriptions and
context string arrays. A process-wide known-secret registry is seeded at initialization
with FI keys and values of credential-like environment variables, using the same
catalog name predicate as transport observation and export sanitization. Transport
hooks always register credential headers and credential/signed-URL query values,
even when model ancestry or a nested hook suppresses wire-span creation. Native
HTTP/1.1 and HTTP/2 `httptrace` hooks also observe headers added by inner auth
RoundTrippers, including wrappers installed before `Init` or holding a saved native
transport. Existing application trace callbacks are composed unchanged. Headers
such as `X-Amz-Security-Token` and `X-Auth-Token` are credential-bearing too.
For concrete `*http.Transport`s passed to `Transport`, or installed as the default
when `Init` runs, native proxy/configuration callbacks also observe HTTP/1.1
request targets and static/dynamic CONNECT authentication headers before a
response exists. Installation wraps `Proxy` and an existing
`GetProxyConnectHeader` callback in place; configure transports before installation
and never install while requests are running. Callbacks run exactly once and
return their original URLs, header maps, errors and panics. Inactive observers
pass through after shutdown. See Known limitations for an opaque wrapper hiding
a separate, never-instrumented native transport.
Suppressed calls inspect only headers/URL; they never buffer bodies. Credential-named fields
in bodies already parsed by an active wire capture are also registered.
The registry holds at most 1024 values of eight or more bytes, with least recently
observed/registered values evicted. Every exported span uses the current registry,
including unrelated traces and descendants started before discovery or after
ancestor retirement. There is no per-span credential inheritance. Structural
credential redaction follows flattened ancestry too, including indexed fields such
as `http.request.header.authorization.0` and `gen_ai.tool.call.arguments.api_key.0`.
Integrations can call `auto.RegisterSecret(value)` for credentials learned outside
a transport. Registry contents never appear in reports, errors or diagnostics.
Replacement is longest-first on immutable export snapshots. The shipping
`internal/wire/privacy.json` and `privacy-fixtures.json` are byte-identical copies
of `spec/auto-instrumentation/`; one catalog-loaded name predicate and one media
predicate serve every recognition site. Credential markers include session/cookie
fields (including `session.id`), while token usage/limits, key identifiers and
logprob-token text retain the catalog's explicit, component-local exemptions.
Inline base64 media uses `<omitted: N base64 chars>`. Magic-byte media is recognized
under any key, including short PNG/JPEG/GIF/WAV/WEBP/MP3/Ogg/FLAC/MP4/PDF values;
only unrecognized base64 in catalog media fields uses the >256-character threshold.
Data URIs of any encoding use exact scalar/query-value boundaries; free text is
scanned without consuming neighboring URLs, query parameters or fragments. MIME
siblings retain exact array-index isolation. Audio payloads of any size, including
numeric samples, are omitted; raw audio/file bytes are never attributes. These
rules cover flattened/bracket-indexed paths, native OTel maps/arrays and JSON
source objects. Safe audio metadata and
transcripts survive; transcripts obey the input hide flags for TTS and output
hide flags for STT. Invocation-parameter and audio-transcript hide controls cover
the root and all flattened/indexed descendants (for example,
`gen_ai.request.parameters.tools.0.description` and `gen_ai.audio.transcript.0`),
without matching similarly named neighboring families.
Content is captured by default: use hide switches when sending sensitive data.
One export-time sanitizer applies these switches and credential/media scrubbing
to **every span forwarded by the agent's exporter**, including application and
native SDK spans: all attribute types, status descriptions, events, links,
resources, scope metadata and tracestate. It sanitizes immutable snapshots;
independent third-party exporters still receive their original data. Nonfinite
numbers become `"NaN"`, `"Infinity"` or `"-Infinity"`; typed byte attributes are
omitted (they may contain raw media). Traversal is bounded to depth 32, 16,384
nodes and 32 MiB of string work per span. If sanitization fails, unchecked
content/metadata is dropped while span identity, timing and status code survive.

## Coverage and dedicated middleware

The embedded byte-identical shared catalog covers OpenAI-compatible chat,
completions/FIM, Responses, embeddings, images, moderation, rerank,
Anthropic Messages, Gemini generation/embedding, Cohere chat/embedding,
Bedrock Converse/invoke, Ollama chat/generate/embed, and HTTP audio STT/TTS for
OpenAI, Groq, Deepgram, ElevenLabs, AssemblyAI and Cartesia. JSON, SSE and
NDJSON parsing follows the shared Python reference. Only endpoints whose
request bodies validate are captured. An unavailable/oversized body is captured
without request content only on catalog-provider hosts, with an unavailable or
truncated marker. Responses requires either `input` or `model`. Model vectors
are never emitted.
Bedrock's binary event stream records request/status, not output messages.

Streams follow the A8/A10 observation boundary. SSE events complete at their
blank line (CRLF, LF or CR line endings), NDJSON records only at their newline
(an unterminated final line never counts, even on clean EOF), and streamed JSON
arrays per complete member. Usage snapshots from those complete frames merge
before one A8 normalization. A standard SSE `event:` name identifies the event
when its JSON omits the redundant type (Cohere v2). `gen_ai.request.stream` comes
from the framing/content type, so a zero-byte or truncated SSE/NDJSON stream is
still a stream. The transport records each non-empty body chunk's arrival time
when its `Read` returns, before copying or parsing (bounded to 4096 records).
`gen_ai.response.time_to_first_chunk` is the first non-empty chunk;
`gen_ai.response.time_to_first_text` is the arrival of the chunk completing the
first frame with non-empty model output (text, refusal, reasoning, tool name or
arguments, code, generated audio) per the spec's qualifying-delta table. Parse
time, span end or first-chunk time are never substituted; without qualifying
output it is omitted.

The hook wraps `http.DefaultTransport`, which covers default `http.Client`s.
Verified in the module cache and against local servers with the actual SDKs:
openai-go/v3 **v3.71.1** starts with `http.DefaultClient` and falls back to
`http.DefaultTransport`; anthropic-sdk-go **v1.82.0** starts with
`http.DefaultClient`. Their default HTTP calls produce wire spans through
`Init`, without adding middleware. Native SDK spans may also exist (notably
Anthropic); this agent does not rewrite those independent spans. Only a
traceAI library span marked `gen_ai.span.kind` suppresses the wire span, including
through intermediate native SDK spans tracked by the agent's processor.

Go cannot monkey-patch libraries. `Report()` labels dedicated instrumentations
**manual**, with these exact one-line integrations (install/import each
corresponding `github.com/future-agi/traceAI/go/<module>` separately):

| Module | One-line integration |
| --- | --- |
| `openai` | `option.WithMiddleware(traceaiopenai.Middleware())` |
| `anthropic` | `option.WithMiddleware(traceaianthropic.Middleware())` |
| `genai` | `genai.ClientConfig{HTTPClient: traceaigenai.NewHTTPClient()}` |
| `eino` | `callbacks.AppendGlobalHandlers(traceaieino.NewHandler())` |
| `a2a` | `a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor())` |

Eino agent/chain/tool steps and A2A RPCs need their manual instrumentors;
wire coverage fills only their underlying model HTTP calls. `Installed` uses
Go build info, not runtime package discovery. A standalone doctor binary cannot
inspect another executable's linked dependencies. Status vocabulary follows
the cross-language contract (`instrumented`, `pending`, `not_installed`,
`missing_instrumentor`, `disabled`, `manual`, `failed`); Go currently emits
`instrumented` for active wire capture, `manual` for library middleware, and
`disabled`/`failed` as appropriate. Absence is separately reflected by
`installed=false`.

No automatic incoming HTTP SERVER spans, framework patching, gRPC, WebSocket,
HTTP/3, realtime audio, Orchestrion weaving or bootstrap installer is shipped.
Custom proxy paths must still match the catalog. Content beyond the capture
limit is not parsed. Responses that callers neither read nor close cannot
complete spans. Multipart uploads are streamed through a counting parser;
files are drained, not retained, and each text field has an independent 64 KiB
cap. Field cardinality and file descriptors are bounded too. Request bodies are
observed only as the real transport reads them: `GetBody` is never called
speculatively, since a user-supplied replay can block before the request is sent.
Transport-requested replays are observed independently without changing retries. Streamed response
reads copy bounded bytes; parsing and span creation occur on EOF/Close. Gzip and
deflate copies are decoded within the body limit; unsupported br/zstd encodings
are marked `traceai.wire.undecoded_encoding`. A midstream transport failure
preserves partial output with ERROR; an early consumer close is OK with
`traceai.wire.incomplete=true`.

Wire spans are CLIENT spans created at completion with explicit request start
time and the request-time parent. Start is transport-wrapper entry, before
matching or copying; end is the observed final byte or transport error, before
parsing/span-processor work. Later EOF reads/Close do not add consumer think time.
`LLM`, `EMBEDDING`, `RERANKER`, and `GUARDRAIL` ancestors suppress capture through
native intermediate spans; `AGENT`, `CHAIN`, and `TOOL` do not. Nested transport
wrappers and explicit suppression do not double-capture. The in-flight marker is
retired on completion; a reused `Response.Request` does not inherit suppression.
The original response object, request identity, trailers and Read/Close results
are preserved; bodies are not read ahead or closed by instrumentation.

## Doctor and tests

```sh
go run github.com/future-agi/traceAI/go/auto/cmd/traceai-doctor
# From this module:
GOTOOLCHAIN=auto go build ./...
GOTOOLCHAIN=auto go test -race ./...
```

Doctor prints masked resolved config, integration statuses/options and coverage.
Collector acceptance is explicitly **not tested**: doctor never sends an
unsolicited test export. Add your own opt-in connectivity test when needed.

The test suite discovers every fixture under the repository's
`spec/auto-instrumentation/fixtures` directory recursively at test time (47
baseline plus the additive `round6/` goldens), replays each with its transport
chunk partition, failure boundary and arrival offsets on a controlled clock,
and checks every expectation. The ten synthetic Bedrock decoded-payload cases
(`milestone-a10-bedrock-*`) run in the decoded-event recognizer harness, as the
spec directs, not as wire cases. It also verifies catalog identity, exercises actual SDK default clients, and builds a separate plain
blank-import application against local provider and OTLP mock servers.
Tests never contact a real AI provider or Future AGI. Deduplication tests use
real active SDK spans with each library-layer kind, rather than requiring the
sibling middleware modules as runtime dependencies. The separate blank-import
fixture waits for batch delivery solely to keep its application code free of
tracing calls; production programs should use `Shutdown` instead.

## Known limitations

As of **2026-10-30 / 2026-10-31** (OTHER-class items, not claims of runtime parity
outside the supported HTTP/1.1 and HTTP/2 provider paths):

- **2026-10-31 — compressed streams and first-text:** when the agent sees encoded
  bytes (the application set `Accept-Encoding` itself, so `net/http` did not
  decompress), chunk arrival offsets are in compressed space. Output and usage
  are still decoded and parsed, but `gen_ai.response.time_to_first_text` is
  omitted rather than estimated. `net/http`'s default transparent gzip path
  observes decoded bytes and is not affected.
- **2026-10-31 — NDJSON without a trailing newline:** per the spec's NDJSON rule a
  record exists only once its newline arrives, so a server that ends the stream
  without a final newline (even on clean EOF) loses that last record's output and
  usage (often Ollama's `done` record). Standard Ollama/Cohere servers terminate
  every record.
- **2026-10-31 — arrival association bound:** at most 4096 non-empty `Read`
  observations are recorded per response. If the first qualifying output frame
  completes after that bound (for example a consumer reading a few bytes at a
  time), `time_to_first_text` is omitted and `traceai.wire.incomplete=true` is
  set (the spec's marker; the Python reference currently names it
  `traceai.wire.timing_truncated`); output, usage and first-chunk time are kept.
- **2026-10-31 — unlisted first-output shapes:** first-text recognizes exactly the
  spec's A10 qualifying-delta table (a port of its oracle). Shapes the spec lists as
  not yet covered (custom-tool chat deltas, streamed transcription deltas, Responses
  shell-tool events, Cohere v1 search queries, SSE speech audio, ElevenLabs
  timestamped NDJSON audio) yield a later or missing first-text; first-chunk,
  output and usage are unaffected.
- **2026-10-31 — Bedrock decoded payloads:** the spec's `milestone-a10-bedrock-*`
  fixtures are verified by the decoded-event recognizer only. Real Bedrock binary
  event streams remain request/status-only, so they carry no first-text milestone.
- **2026-10-31 — immediate return without Shutdown:** a blank-import program that
  returns from `main` right after its AI call can exit before the 200 ms batch
  export; the startup hint and the example above give the fix (`auto.Shutdown`).
  A launcher-generated shutdown call is the fi CLI's responsibility. The hint is
  printed by any binary that links the package and activates with FI credentials,
  including `traceai-doctor` (stderr only).
- **2026-10-31 — media hints across attribute families:** projection pins direct
  MIME/type hint keys to their payload keys, and all context attributes (protected
  identity included) are scrubbed jointly. No fallback restores an unchecked
  input value: when hide-images classifies a whole attribute object as one image
  block, its protected keys keep the joint walk's scrubbed form (so a
  credential-marked `session.id` stays redacted as above) and everything else is
  hidden; a protected scalar whose own value is an image URL is not an image
  path and is kept, as before. The A7 protected set is closed: exact
  identity/outcome/provenance keys, the four FI routing keys (`project_name`,
  `project_type`, `project_version_name`, `project_version_id`), `server.address`/
  `server.port`, and numeric `gen_ai.usage.*` counters. Other `project_*`,
  `server.*` or non-numeric `gen_ai.usage.*` attributes are ordinary metadata
  (fitted after content, kept with their MIME context). A hint that exists only
  inside a JSON-encoded context string and governs a separate wire attribute path
  (or the reverse) is applied by the export-time sanitizer only when both survive
  projection. Use the hide controls for such application-specific layouts.
- **2026-10-31 — service.name default (A6 change):** an agent-owned provider without
  `OTEL_SERVICE_NAME`/`OTEL_RESOURCE_ATTRIBUTES` now reports OpenTelemetry's
  `unknown_service:<executable>` instead of the working-directory name; the FI
  project (`project_name`) is unchanged. A host provider's `service.name` is no
  longer replaced in the FI view by `OTEL_SERVICE_NAME` or `WithConfig`.

- **2026-10-30 — host attribute limits:** The pinned OTel SDK exposes no public
  provider-limit getter. The agent reads its immutable numeric limit once using
  a narrow, read-only reflection adapter (no unsafe access or synthetic spans),
  covered by provider-configuration regressions. An unrecognized SDK layout
  fails closed to zero attributes with a bounded warning. Zero-slot providers
  cannot retain attribute-based identity or degradation markers; span IDs,
  status and timing remain structural. The application provider is never changed.
- **2026-10-30 — opaque, uninstrumented native transports (OTHER):** An auth
  `RoundTripper` or closure can hide a saved `*http.Transport` that was neither
  the default at `Init` nor passed to `auto.Transport`. Credentials added inside
  that hidden transport did not cross an instrumented transport; its HTTP/1.1
  request target (including a late `client_secret` query) and CONNECT headers
  are not exposed by public `httptrace` callbacks. A credential added there can
  remain unregistered even on EOF and
  later appear unchanged in an unrelated application span. Pass the actual
  concrete native transport to `auto.Transport` before building the auth wrapper,
  or call `auto.RegisterSecret`. Static and
  dynamic CONNECT headers on concrete transports the agent does hook are
  observed. Use `FI_HIDE_INPUTS`/`FI_HIDE_OUTPUTS` and the text/message hide
  controls for sensitive errors and payloads. Re-confirmed unchanged on
  2026-10-31 (an independent verifier reproduced it with a hidden transport and
  confirmed explicit `auto.Transport` registration closes it).
- **2026-10-30 — credential scope:** Secrets transformed by application code before
  telemetry (signature extraction into plain text, JSON encoded inside strings,
  numeric re-typing, base64 or splitting), and unmarked credentials that never
  crossed an instrumented transport, cannot always be recognized. Use
  `FI_HIDE_INPUTS`/`FI_HIDE_OUTPUTS`, the text/message/image hide switches above,
  or explicitly call `auto.RegisterSecret` with the actual string to be exported.
- **2026-10-30 — manual paths and retention:** Without the agent's transport hooks,
  only FI keys, credential-like environment values present at initialization and
  explicit `auto.RegisterSecret` calls seed value-based masking. Late environment
  changes are not rescanned. The 1024-value LRU may evict older credentials; values
  shorter than eight bytes are not registered (credential-named fields still
  redact structurally). Already-exported telemetry cannot be scrubbed retroactively.
  Keep transport hooks enabled and use the hide controls for sensitive workloads.
- **2026-10-30 — privacy work budgets:** The catalog limits a walk to 4 MiB of
  text, 16,384 nodes and depth 32. Repeated raw/decoded/flattened representations
  share that budget, so even a request below the body limit may lose captured
  content. Wire capture retains span identity, timing, outcome, protocol metadata
  and usage, with `traceai.wire.privacy_degraded=true`; unchecked content is never
  used as a fallback. An invalid/over-budget identity retains only fixed wire
  provenance and the degradation marker. **2026-10-31:** when the application's
  context attributes exhaust the budget, their content is dropped and protected
  context keys (`user.id`, `project_name`, ...) are kept only after a bounded
  re-check together with every MIME/type hint of that context (then scalar keys
  alone); a protected value that still cannot be checked is dropped, never
  restored unchecked.
- Go has no atexit hook. Abrupt exit and SIGTERM without application-managed
  `Shutdown` can lose queued spans; the agent deliberately installs no signal
  handlers. Read/close response bodies and flush in your graceful-shutdown path.
- Go cannot patch compiled libraries. Dedicated middleware, custom transports,
  incoming SERVER spans and third-party private context/suppression keys require
  explicit integration. Replacing the global provider after first initialization
  cannot be detected or rewired; `Init` is once-only.
- Response bodies remain `io.ReadCloser` observers, not the original concrete body
  type. Arbitrary user-defined body extension methods/type assertions are not
  forwarded. HTTP 101 upgrades are passed through untouched and are not captured;
  WebSocket, HTTP/3 and bidirectional/realtime protocols are not instrumented.
- Unknown compression (`br`/`zstd`) and Bedrock binary event streams retain
  request/status metadata rather than parsed output. Capture and sanitizer limits
  intentionally omit oversize/deep content. Unread, never-closed bodies cannot
  produce completed spans and retain their bounded capture until released by GC.
- Covered-ancestry deduplication requires recording spans on the provider the
  agent's processor observes (up to 256 live ancestor steps). Model spans on a
  different/private provider, or kinds set only after the HTTP call, cannot be
  inferred across native intermediate spans. Direct model parents are recognized.
- OpenTelemetry Go 1.47's OTLP encoder omits link tracestate. The sanitizer still
  scrubs parent/span/link tracestate in the immutable custom-processor view; only
  span tracestate is serialized by that upstream encoder. Sanitized vendor entries
  may be reordered in export views; application contexts remain unchanged.
- Doctor is local/network-free: it reports configuration and linked build-info
  dependencies, not another executable's dependencies or collector acceptance.
- Native HTTP timeout parity uses a small context-capability adapter named
  `*http2.Transport`, recognized by Go 1.26's `net/http` transport-type heuristic.
  Go provides no public context-capability marker for wrappers. This is not an
  HTTP/2 protocol implementation; native HTTP/1.1 and HTTP/2 still use the original
  transport. Future toolchains changing that heuristic require revalidation.
  Custom alternate protocols registered on `http.Transport` that ignore request
  contexts are outside the supported native HTTP/1.1/HTTP/2 paths; use a separate
  custom `RoundTripper` so its legacy cancellation path is retained.
