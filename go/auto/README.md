# traceAI Go auto

HTTP-level AI tracing for Go, with bounded capture, OpenTelemetry spans and the
same wire catalog and golden fixtures as traceAI's other auto agents. This is
an alpha transport agent, not runtime bytecode instrumentation: Go requires one
import and a rebuild. Dedicated library middleware remains opt-in.

## Quick start

1. Add the module. Until its first go/auto tag is released, use `@dev` instead
   of `@latest`:

   ```sh
   go get github.com/future-agi/traceAI/go/auto@dev
   ```

2. Import it once, anywhere in your program, and flush before `main` returns:

   ```go
   import (
       "context"
       "time"

       "github.com/future-agi/traceAI/go/auto"
   )

   func main() {
       defer func() {
           ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
           defer cancel()
           _ = auto.Shutdown(ctx) // Go has no exit hook; this sends queued spans.
       }()
       // Your code. AI calls through http.DefaultTransport, or clients built
       // on it, are traced without any other change.
   }
   ```

   A long-running service can use `import _ "github.com/future-agi/traceAI/go/auto"`
   instead and call nothing.

3. Set your Future AGI keys and rebuild:

   ```sh
   export FI_API_KEY=... FI_SECRET_KEY=...
   export FI_PROJECT_NAME=my-service
   go run .
   ```

Each AI call becomes one CLIENT span with the model, messages, usage and timing,
after the privacy rules below. Only AI spans leave the process for Future AGI:
these wire spans, and spans from traceAI modules (marked `gen_ai.span.kind` or
`fi.span.kind`) on an SDK tracer provider the agent attaches to (see below).
AI spans from other instrumentation libraries are not forwarded unless they set
`gen_ai.span.kind`. Your other spans, such as HTTP server, database or
application spans, are never sent to Future AGI. [`example/main.go`](example/main.go) is a
complete program; `go run github.com/future-agi/traceAI/go/auto/cmd/traceai-doctor`
prints the resolved configuration with keys masked.

**Your OpenTelemetry setup stays yours.** The agent never replaces the global
tracer provider. It starts on the first AI call (or an explicit `auto.Init`,
whichever comes first) and then attaches to your global SDK provider if you
have installed one, so its wire spans join your traces; otherwise it keeps a
private provider that only its wire spans use. A tracer you cache at package
level keeps working. To use options such as `auto.WithTracerProvider(tp)`, call
`auto.Init(ctx, options...)` before the first AI call. When the agent attaches
to your provider, its wire spans, with their captured content, also go to that
provider's own exporters. `FI_HIDE_*` applies to both copies; known-secret
replacement applies only to the copy sent to Future AGI.

**traceAI module spans need an SDK tracer provider.** The modules (for example
`traceaieino` or `traceailangchaingo`) use the global tracer provider unless you
pass them one, and their spans reach Future AGI only through an SDK tracer
provider the agent attaches to. With only the import and no provider of your
own, module spans go nowhere. Install one as the global provider before the
first AI call, or pass it to `auto.Init` with `auto.WithTracerProvider(tp)` (and
to each module's own `WithTracerProvider` option):

```go
tp := sdktrace.NewTracerProvider() // sdktrace "go.opentelemetry.io/otel/sdk/trace"
otel.SetTracerProvider(tp)         // before the first AI call
```

Module spans that end before the agent activates are not forwarded, so call
`auto.Init` at startup if a step can finish before the first AI call.

**`http.DefaultTransport` changes type.** With FI keys set and wire capture on,
package initialization replaces `http.DefaultTransport` with the agent's wrapper,
which is not an `*http.Transport`. An unchecked
`http.DefaultTransport.(*http.Transport)` assertion, for example
`http.DefaultTransport.(*http.Transport).Clone()`, then panics; a checked
assertion (`t, ok := ...`) takes its fallback. Build custom transports from
`&http.Transport{...}` and wrap them with `auto.Transport`, or turn wire capture
off with `FI_WIRE_CAPTURE=false` (`wire_capture = false` in `futureagi.toml`) if
a dependency makes the unchecked assertion.

The rest of this page is the detailed reference.

## Install and run

Requires Go 1.26 (the OpenTelemetry 1.47 dependency minimum). With a recent Go
installation, `GOTOOLCHAIN=auto` downloads the required toolchain. The module
requires only OpenTelemetry, protobuf and `golang.org/x/net`; it does not add
or upgrade any AI SDK in your build.

Add the blank import before building; no per-call tracing code is needed:

```go
import _ "github.com/future-agi/traceAI/go/auto"
```

Or use the named import and one-line explicit initialization:

```go
shutdown, err := auto.Init(ctx)
```

Initialization is fail-open, network-free, and never installs signal handlers
or a global tracer provider. Package initialization only arms wire capture,
and only when the environment or config file has a complete FI pair, wire
capture is on and the agent is not disabled: it wraps `http.DefaultTransport`
and nothing else. The agent then activates once, at the first explicit `Init`
or the first captured AI request, whichever comes first. Activation uses, in
this order, the `WithTracerProvider` provider, the application's global SDK
provider if one is installed by then, or a private provider for the agent's
wire spans. A failed activation leaves requests passing through unchanged,
with one warning. Without FI credentials, an explicit `Init` still activates
when the application supplies an SDK tracer provider (explicitly or as the
existing global provider) or a span processor. With none of these
destinations, it warns once and leaves the default transport and tracer
provider unchanged. An invalid explicit configuration returns an error;
importing the package never propagates an error to the app.
Collector failures are bounded by the batch queue and exporter timeout, emit a
single warning, and drop failed batches rather than interrupting application
traffic. Errors and warnings do not contain credential values or request URLs.

**Go has no atexit.** Short-lived programs must flush explicitly. A blank import
cannot ensure delivery at exit: a program that returns from `main` right after
its last AI call can exit with that span still queued. When package
initialization arms wire capture with a complete FI pair, it prints one
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
  once per process. Call it before the first AI call: the first captured AI
  request activates the agent with environment and config-file settings, and
  from then on, like any later call, `Init` returns the original shutdown
  function, ignores options without evaluating them, and logs one line (once).
  Those calls never replace the provider, processor, resource, sampling policy
  or transports. A package initialization that cannot arm (missing credentials,
  disabled or invalid configuration) leaves the first successful explicit
  initialization available. Disable-after-enable and restart-after-shutdown are
  unsupported.
- `Shutdown(ctx)` removes the agent's processor and restores its default
  transport, but does not shut down a provider owned by your application.
  Called before the first AI call, it only removes the armed wrapper.
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
  context attributes to child wire spans. The session id set with
  `ContextWithSessionID` is exported as its value, with known secrets and
  credential-shaped values inside it still redacted; a `session.id` passed to
  `ContextWithAttributes`, or set or replaced on the wire span by another span
  processor, is redacted like any credential-named key. Other
  modules' context keys are private, so their session/user helpers do not
  automatically bridge to this package. OTel parent span context still works
  across modules.

Options include `WithCredentials`, `WithProject`, `WithSampleRate`,
`WithWireCapture`, `WithTraceConfig`, `WithTracerProvider`, `WithSpanProcessor`,
and `WithConfig` (a complete replacement, not a sparse merge). Options apply
only to the call that activates the agent, so pass them before the first AI
call. A custom span processor replaces the agent's network exporter and does
not need credentials; on an application-owned provider it receives every span
of that provider, through the same privacy sanitizer.
An application-owned SDK provider is also sufficient without FI credentials:
no FI exporter is created, wire spans go to its existing processors, and
`ForceFlush` delegates to that provider. The host owns its privacy/export policy;
agent shutdown only removes observation hooks, never shuts down host processors.

The agent never calls `otel.SetTracerProvider`. If a `*sdktrace.TracerProvider`
is passed with `WithTracerProvider`, or is the global provider when the agent
activates, the agent adds its processor; existing exporters and the
application's sampling policy are untouched. On such an application-owned
provider the Future AGI exporter forwards only AI spans: the agent's wire spans
and spans that carry `gen_ai.span.kind` or `fi.span.kind`. Every other span
still reaches the application's own exporters and never Future AGI. Future AGI
project resource attributes are applied to copies sent to
its exporter, not to other exporters. The application's resource identity is
authoritative in that copy too (A6): every application resource attribute,
including `service.name`, is kept exactly, even when `OTEL_SERVICE_NAME`,
`WithConfig` or `FI_PROJECT_NAME` differ. FI routing is the separate
`project_name`/`project_*` attributes; other agent defaults only fill keys the
application did not set. An agent-owned provider uses normal OpenTelemetry
resource detection (`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, SDK identity,
`unknown_service:<executable>` fallback); the FI project or its working-directory
default is never used as `service.name`. If no SDK provider exists when the
agent activates, it creates a private, parent-based ratio-sampled provider that
only its wire spans use; it is never made global, and wire spans keep their
request-time parent, so they stay in the application's traces. An installed
third-party API provider is not replaced. A global provider installed *after*
activation does not receive the agent's processor, and a second `Init` cannot
attach it. Install your provider before the first AI call, or pass it with
`WithTracerProvider` to an `Init` before the first AI call.

Do not initialize, shut down or replace `http.DefaultTransport` concurrently
with HTTP requests. While wire capture is on, `http.DefaultTransport` is the
agent's wrapper, not an `*http.Transport` (see the quick start). Existing
clients that cached a different concrete transport need `Transport(base)`;
`Transport` also works before activation, and such clients activate the agent
like the default transport. Cached agent wrappers stop capturing after
shutdown; calling `Transport(cached)` then returns its uninstrumented base, not
a new agent.

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
| `FI_INSTRUMENTATIONS` | all | Comma-separated allowlist of `wire`, `openai`, `anthropic`, `genai`, `eino`, `a2a`, `bedrock`, `langchaingo`, `lambda` |
| `FI_DISABLED_INSTRUMENTATIONS` | none | Comma-separated denylist; `wire` disables all transport capture |
| `FI_WIRE_CAPTURE` | `true` | Wire-layer switch; `false` also keeps `http.DefaultTransport` an `*http.Transport` |
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
A hide switch, or its `futureagi.toml` setting, reads `true`, `1`, `yes` or `on`
as hide and `false`, `0`, `no` or `off` as show, ignoring case and surrounding
spaces; a blank value counts as not set. Any other value hides that content and
prints one warning that names the variable, so a mistyped switch never leaves
content visible. `FI_WIRE_CAPTURE`, `FI_AUTO_DEBUG` and `OTEL_SDK_DISABLED` are
not hide switches: they are on only for `true`, case-insensitively. Inputs/outputs are
redacted with `__REDACTED__`; messages are dropped or text-redacted. This module
also redacts raw JSON under text-hide switches and audio transcripts when
content is hidden, so the same content cannot leak through a second attribute.
HTTP headers are never attributes. Credential-shaped JSON keys at any depth,
signed URL query values, and echoed request credentials/FI keys (at least eight
characters) are replaced with `<redacted>`, including in error descriptions and
context string arrays. A process-wide known-secret registry is seeded at activation
with FI keys and values of credential-like environment variables, using the same
catalog name predicate as transport observation and export sanitization. Transport
hooks always register credential headers and credential/signed-URL query values,
even when model ancestry or a nested hook suppresses wire-span creation. Native
HTTP/1.1 and HTTP/2 `httptrace` hooks also observe headers added by inner auth
RoundTrippers, including wrappers installed before `Init` or holding a saved native
transport. Existing application trace callbacks are composed unchanged. Headers
such as `X-Amz-Security-Token` and `X-Auth-Token` are credential-bearing too.
For concrete `*http.Transport`s passed to `Transport`, or installed as the default
when wire capture is armed or `Init` runs, native proxy/configuration callbacks also observe HTTP/1.1
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
fields (including `session.id` from headers, bodies, other attributes and other
span processors; only the session id the agent writes from `ContextWithSessionID`
keeps its value, and only while the wire span still carries that value, with
known secrets and credential shapes in it still redacted and a value cut by a
value-length limit scrubbed like any other cut value), while token usage/limits, token timing and
throughput names (`time_to_first_token`, `tokens_per_second`, ...) and key
identifiers retain the catalog's explicit, component-local exemptions. A `token`
field inside logprobs is redacted like any other token field. Catalog value
shapes (long `sk-` keys, AWS access key IDs, JWTs, `Bearer`/`Basic` values and
private-key PEM blocks) are redacted in any text, whatever the key; the
`Bearer`/`Basic` shape is case-insensitive, so `basic` followed by a word of
eight or more letters is redacted too. The exact usage counters the catalog
declares as metrics, such as `gen_ai.usage.input_tokens.audio`, keep valid
numeric values even though their names contain `audio`.
Inline base64 media uses `<omitted: N base64 chars>`. Magic-byte media is recognized
under any key, including short PNG/JPEG/GIF/WAV/WEBP/MP3/Ogg/FLAC/MP4/PDF values;
only unrecognized base64 in catalog media fields uses the >256-character threshold.
Data URIs of any encoding use exact scalar/query-value boundaries; free text is
scanned without consuming neighboring URLs, query parameters or fragments. MIME
siblings retain exact array-index isolation. An image or video `type`
(`image`, `input_image`, `image_url`, `video`, `input_video`) or `modality`
establishes the same media context as an image or video MIME type; under an
image declaration, encoded `image`, `image_url` and `url` values are omitted
while plain HTTP image URLs stay text. Audio payloads of any size, including
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
to **every span the agent forwards**: wire spans and traceAI module spans (on an
SDK tracer provider the agent attaches to) to the Future AGI exporter, and every
span handed to a custom span processor. It
covers all attribute types, status descriptions, events, links, resources,
scope metadata and tracestate. It sanitizes immutable snapshots;
independent third-party exporters still receive their original data. Nonfinite
numbers become `"NaN"`, `"Infinity"` or `"-Infinity"`; typed byte attributes are
omitted (they may contain raw media). Traversal is bounded to depth 32, 16,384
nodes and 32 MiB of string work per span. If sanitization fails, unchecked
content/metadata is dropped while span identity, timing and status code survive.

### Privacy differences from other traceAI SDKs

The agent embeds a byte-identical copy of the shared privacy catalog (version 3)
and runs its shared fixtures. Four rules differ. The first three are catalog
rules that are not ported yet, and in each Go removes more than the other SDKs.
In the fourth, by design, Go removes less:

- **Logprob tokens:** the provider-response policy for logprob tokens is not
  ported. Every `token` field inside logprobs is redacted by name, so logprob
  text is lost.
- **Credential source ownership:** known secrets are replaced longest first,
  rather than by the catalog's ownership rules across JSON and URL syntax.
- **Zero-valued usage details:** zero-valued detail counters stay protected
  instead of yielding attribute capacity to content.
- **Session id from `auto.ContextWithSessionID`:** by design, the session id
  set with `auto.ContextWithSessionID` is exported as its value, although the
  catalog treats session names as credentials. Known secrets and credential
  shapes inside it are still redacted. A `session.id` from any other source is
  redacted by name.

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
Verified against local servers with the actual SDKs in the separate
`sdkcompat` test module (so this module never requires them):
openai-go/v3 **v3.71.1** starts with `http.DefaultClient` and falls back to
`http.DefaultTransport`; anthropic-sdk-go **v1.82.0** starts with
`http.DefaultClient`. Their default HTTP calls produce wire spans through
`Init`, without adding middleware. Native SDK spans may also exist (notably
Anthropic); this agent does not rewrite those independent spans. Only a
traceAI library span marked `gen_ai.span.kind` suppresses the wire span, including
through intermediate native SDK spans tracked by the agent's processor.

Go cannot monkey-patch libraries. `Report()` labels dedicated instrumentations
**manual**, with these exact one-line integrations (install/import each
corresponding `github.com/future-agi/traceAI/go/<module>` separately). Their
spans reach Future AGI only through an SDK tracer provider the agent attaches
to: install one as the global provider before the first AI call, or pass it to
`auto.Init` with `auto.WithTracerProvider(tp)` and to the module's own
`WithTracerProvider` option (see the quick start). Without one, module spans
go nowhere.

| Module | One-line integration |
| --- | --- |
| `openai` | `option.WithMiddleware(traceaiopenai.Middleware())` |
| `anthropic` | `option.WithMiddleware(traceaianthropic.Middleware())` |
| `genai` | `genai.ClientConfig{HTTPClient: traceaigenai.NewHTTPClient()}` |
| `eino` | `callbacks.AppendGlobalHandlers(traceaieino.NewHandler())` |
| `a2a` | `a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor())` |
| `bedrock` | `traceaibedrock.NewClient(bedrockruntimeClient)` |
| `langchaingo` | `openai.WithCallback(traceailangchaingo.NewHandler())` |
| `lambda` | `lambda.StartHandler(traceailambda.WrapHandler(handler, provider))` |

Eino agent/chain/tool steps, LangChainGo chain, tool and retriever steps, A2A
RPCs and Lambda invocations need their manual instrumentors;
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
# Real OpenAI and Anthropic SDK clients, from sdkcompat/ (a separate test module):
GOTOOLCHAIN=auto go test -race ./...
```

`TRACEAI_TEST_HOST_GATE` may name a wrapper script that the end-to-end tests use
to start child builds on a shared machine; leave it unset everywhere else.

Doctor prints masked resolved config, integration statuses/options and coverage.
Collector acceptance is explicitly **not tested**: doctor never sends an
unsolicited test export. Add your own opt-in connectivity test when needed.

The test suite discovers every fixture under the repository's
`spec/auto-instrumentation/fixtures` directory recursively at test time (47
baseline plus the additive `round6/` goldens), replays each with its transport
chunk partition, failure boundary and arrival offsets on a controlled clock,
and checks every expectation. The ten synthetic Bedrock decoded-payload cases
(`milestone-a10-bedrock-*`) run in the decoded-event recognizer harness, as the
spec directs, not as wire cases. It also verifies catalog identity, runs all
shared privacy fixtures, and builds the shipped example and separate plain
blank-import applications against local provider and OTLP mock servers. The
`sdkcompat` module exercises actual SDK default clients.
Tests never contact a real AI provider or Future AGI. Deduplication tests use
real active SDK spans with each library-layer kind, rather than requiring the
sibling middleware modules as runtime dependencies. The separate blank-import
fixture waits for batch delivery solely to keep its application code free of
tracing calls; production programs should use `Shutdown` instead.

## Known limitations

These limitations apply to the supported HTTP/1.1 and HTTP/2 provider paths;
nothing here claims runtime parity outside them.

- **`http.DefaultTransport` changes type:** with FI keys set and wire capture
  on, package initialization replaces `http.DefaultTransport` with the agent's
  wrapper, which is not an `*http.Transport`. An unchecked
  `http.DefaultTransport.(*http.Transport)` assertion in the application or a
  dependency panics from then on; a checked assertion takes its fallback. Build
  custom transports from `&http.Transport{...}` and wrap them with
  `auto.Transport`, or set `FI_WIRE_CAPTURE=false` (`wire_capture = false` in
  `futureagi.toml`). After a failed activation on the first AI call the wrapper
  stays in place and passes requests through, so the type change remains.
- **Activation on the first AI call:** with only the blank import, the agent
  activates inside the first request that matches the wire catalog. Options
  passed to a later `Init` are ignored, a global provider installed after that
  request does not receive the agent's processor, and credentials seen in
  earlier non-AI requests are not in the known-secret registry (credential
  names and shapes are still redacted). traceAI module spans on an SDK provider
  the agent attaches to that started before activation are still forwarded when
  they end, but cannot suppress a wire span through intermediate native spans;
  those that end before activation are not forwarded.
- **Compressed streams and first-text:** when the agent sees encoded
  bytes (the application set `Accept-Encoding` itself, so `net/http` did not
  decompress), chunk arrival offsets are in compressed space. Output and usage
  are still decoded and parsed, but `gen_ai.response.time_to_first_text` is
  omitted rather than estimated. `net/http`'s default transparent gzip path
  observes decoded bytes and is not affected.
- **NDJSON without a trailing newline:** per the spec's NDJSON rule a
  record exists only once its newline arrives, so a server that ends the stream
  without a final newline (even on clean EOF) loses that last record's output and
  usage (often Ollama's `done` record). Standard Ollama/Cohere servers terminate
  every record.
- **Arrival association bound:** at most 4096 non-empty `Read`
  observations are recorded per response. If the first qualifying output frame
  completes after that bound (for example a consumer reading a few bytes at a
  time), `time_to_first_text` is omitted and `traceai.wire.incomplete=true` is
  set (the spec's marker; the Python reference currently names it
  `traceai.wire.timing_truncated`); output, usage and first-chunk time are kept.
- **Unlisted first-output shapes:** first-text recognizes exactly the
  spec's A10 qualifying-delta table (a port of its oracle). Shapes the spec lists as
  not yet covered (custom-tool chat deltas, streamed transcription deltas, Responses
  shell-tool events, Cohere v1 search queries, SSE speech audio, ElevenLabs
  timestamped NDJSON audio) yield a later or missing first-text; first-chunk,
  output and usage are unaffected.
- **Bedrock decoded payloads:** the spec's `milestone-a10-bedrock-*`
  fixtures are verified by the decoded-event recognizer only. Real Bedrock binary
  event streams remain request/status-only, so they carry no first-text milestone.
- **Immediate return without Shutdown:** a blank-import program that
  returns from `main` right after its AI call can exit before the 200 ms batch
  export; the startup hint and the example above give the fix (`auto.Shutdown`).
  A launcher-generated shutdown call is the fi CLI's responsibility. The hint is
  printed by any binary that links the package and arms wire capture with FI
  credentials, including `traceai-doctor` (stderr only).
- **Media hints across attribute families:** projection pins direct
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
  (fitted after content, kept with their MIME context). Projection also
  pins hints in every representation the export sanitizer reads
  (direct strings, string arrays, native maps, generic slices and JSON object
  strings), so a payload is never admitted without its hint; a hint value too
  large to index is pinned to every group (fail-closed).
- **Span limits and media context:** the SDK
  reports dropped attribute counts but never value truncation. The export
  boundary reads the provider's value-length limit (the same read-only adapter
  as the count limit; an unreadable layout fails closed to 0) and decides, per
  attribute object (span, each event, each link), which parts may have lost
  MIME/type context: every path when the SDK dropped any of its attributes;
  otherwise, for span attributes, the hint-parent subtree of each value with
  at least the limit's length in runes (a cut MIME/type value is never a safe
  one). The pinned SDK does not length-truncate events or links, so their
  complete values keep precise catalog recognition. Inside an
  at-risk part EVERY non-empty value (string of any length or alphabet, raw
  bytes, number/bool scalar or array) under a media-capable path is omitted: a
  catalog MIME payload, audio, audio payload or unrecognized-base64 field name
  and its indexed/flattened descendants, up to an audio metadata field such as
  `transcript` or `id` (a capable field below that starts again). A possibly
  cut string whose decodable base64 prefix is consistent with catalog magic
  (PNG, WAV, MP4, ...) is omitted at any path.
  "Consistent" means some continuation could satisfy a catalog rule, including
  a prefix that ends before the signature (an MP4 `ftyp` box size, all that
  survives when CR/LF wrapping uses the length budget), so every cut retaining
  a payload character of catalog magic media is omitted. As a consequence, any cut
  base64-alphabet value with at most 6 data characters (CR/LF skipped, up to
  the first `=`) is omitted at any path, e.g. CR/LF-separated alphanumeric
  text under a limit of 8, or every such value under a limit of 6 or less.
  Cut status also follows strings into URL query
  values, prose and decoded JSON tokens, before rewriting can shorten the
  evidence. Query payload fields fail closed when a later MIME/type hint may
  have been cut off; a final magic prefix also fails closed, including cuts
  inside a percent escape or an unfinished JSON slash/Unicode escape. Ordinary
  query payload fields in such strings may
  therefore be omitted too; count-only loss does not truncate a URL's internal
  query object. Prefixes retaining only 1–3
  base64 characters also fail closed. These occur both under very small value
  limits and inside URL query values under ordinary limits (e.g. 28–30), where
  the URL prefix consumes most of the attribute budget.
  A possibly cut string also
  receives a bounded lexical scan independent of JSON/MIME reconstruction:
  every base64-alphabet run of 8+ characters (standard or URL-safe, with or
  without padding, including an undecodable final quantum) and every `data:`
  URI fragment is replaced with its usual omission marker. This applies inside
  quotes and nested JSON strings, and at the cut's very end. The attribute and
  its OTel representation are retained. As a consequence, an all-letter or
  all-digit run of that length is indistinguishable from base64 and is omitted
  in a cut value too; short words, punctuation and non-base64 text survive.
  Untruncated values retain precise catalog behavior, and cut state does not
  spread to their sibling values. Full magic recognition also checks the
  decodable prefix when a trailing partial quantum has length 1 modulo 4.
  Whole registered
  secrets are masked before the lexical scan, which preserves `<redacted>`;
  rewriting a long base64-shaped segment cannot expose the secret's shorter
  punctuated segments. Decoded quoted tokens inherit their enclosing value's
  cut state, including magic prefixes with only 1–7 payload characters.
  Go's SDK truncates application attributes inside `SetAttributes` with no
  hook, so application spans are recognized at this export boundary. Agent wire spans walk each wire
  value with every context and wire MIME/type hint at capture, before
  projection and the SDK, and the capture-time protected-context re-check is
  itself fail-closed. Known consequences: in such an object ordinary
  text under those paths (`content`, `value`, `data`, `body`, `delta`, ...,
  e.g. message contents or `output.value`) is exported as an omission marker,
  and a truncated JSON string with a number/bool under one becomes
  `<omitted: media>` as a whole; a value exactly at the limit is
  indistinguishable from a cut one. Event/link objects still fail closed on
  dropped attribute counts, but never merely on a span value-length limit. Raise
  the host's attribute limits to keep that content. The SDK's collection depth
  limit (default 64) is not reported either; it removes whole nested
  collections, so a scalar MIME sibling and its payload are kept or removed
  together. Host-owned exporters keep the application's original view.
- **service.name default:** an agent-owned provider without
  `OTEL_SERVICE_NAME`/`OTEL_RESOURCE_ATTRIBUTES` reports OpenTelemetry's
  `unknown_service:<executable>`, not the working-directory name; the FI
  project (`project_name`) is separate. A host provider's `service.name` is
  never replaced in the FI view by `OTEL_SERVICE_NAME` or `WithConfig`.
- **Host attribute limits:** The pinned OTel SDK exposes no public
  provider-limit getter. The agent reads its immutable numeric limits once using
  a narrow, read-only reflection adapter (no unsafe access or synthetic spans),
  covered by provider-configuration regressions that CI also runs against the
  latest OpenTelemetry SDK. An unrecognized SDK layout fails closed to zero
  attributes and treats every exported value as cut, with a one-time warning
  that names the OpenTelemetry SDK. Zero-slot providers
  cannot retain attribute-based identity or degradation markers; span IDs,
  status and timing remain structural. The application provider is never changed.
- **Opaque, uninstrumented native transports:** An auth
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
  controls for sensitive errors and payloads. Explicit `auto.Transport`
  registration of the hidden transport closes this gap.
- **Credential scope:** Secrets transformed by application code before
  telemetry (signature extraction into plain text, JSON encoded inside strings,
  numeric re-typing, base64 or splitting), and unmarked credentials that never
  crossed an instrumented transport, cannot always be recognized. Use
  `FI_HIDE_INPUTS`/`FI_HIDE_OUTPUTS`, the text/message/image hide switches above,
  or explicitly call `auto.RegisterSecret` with the actual string to be exported.
- **Manual paths and retention:** Without the agent's transport hooks,
  only FI keys, credential-like environment values present at activation and
  explicit `auto.RegisterSecret` calls seed value-based masking. Late environment
  changes are not rescanned. The 1024-value LRU may evict older credentials; values
  shorter than eight bytes are not registered (credential-named fields still
  redact structurally). Already-exported telemetry cannot be scrubbed retroactively.
  Keep transport hooks enabled and use the hide controls for sensitive workloads.
- **Privacy work budgets:** The catalog limits a walk to 4 MiB of
  text, 16,384 nodes and depth 32. Repeated raw/decoded/flattened representations
  share that budget, so even a request below the body limit may lose captured
  content. Wire capture retains span identity, timing, outcome, protocol metadata
  and usage, with `traceai.wire.privacy_degraded=true`; unchecked content is never
  used as a fallback. An invalid/over-budget identity retains only fixed wire
  provenance and the degradation marker. When the application's
  context attributes exhaust the budget, their content is dropped and protected
  context keys (`user.id`, `project_name`, ...) are kept only after a bounded
  re-check together with every MIME/type hint of that context (then scalar keys
  alone); a protected value that still cannot be checked is dropped, never
  restored unchecked. That re-check runs fail-closed (see span limits
  above), so samples whose hint was a native map, JSON string or generic slice
  are omitted too.
- Go has no atexit hook. Abrupt exit and SIGTERM without application-managed
  `Shutdown` can lose queued spans; the agent deliberately installs no signal
  handlers. Read/close response bodies and flush in your graceful-shutdown path.
- Go cannot patch compiled libraries. Dedicated middleware, custom transports,
  incoming SERVER spans and third-party private context/suppression keys require
  explicit integration. A global provider installed after activation cannot be
  detected or rewired; `Init` is once-only.
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
