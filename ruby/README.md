# traceai-auto

Ruby >= 3.1 zero-code AI HTTP tracing. Gem name `traceai-auto`; require path
`traceai/auto`. This is a new Ruby implementation, not a claim of production
parity with every traceAI library instrumentor. See **Verification and limits**.

## Use

Install the gem in the application's bundle. It has no runtime gem dependencies:
even gemspec dependencies would activate before a RUBYOPT shim can defer them.
The optional `opentelemetry-sdk` is used only after the application has selected
its versions; the upstream protobuf exporter is used by the tests, not the agent:

```sh
export FI_API_KEY=... FI_SECRET_KEY=...
export RUBYOPT="${RUBYOPT:+$RUBYOPT }-rtraceai/auto"
bundle exec ruby app.rb
```

The application's source needs no tracing code. For a source/embedded install,
add `-I/absolute/path/to/ruby/lib` to `RUBYOPT` before `-rtraceai/auto`. The universal
launcher uses this same require path. Do not replace an existing `RUBYOPT`.

Explicit initialization without eager defaults:

```ruby
require 'traceai/auto/agent'
TraceAI::Auto.init(project_name: 'checkout', sample_rate: 0.5)
```

FI keys are optional when the application supplies an OpenTelemetry SDK destination:

```ruby
require 'opentelemetry/sdk' # selected by the application's own bundle
require 'traceai/auto/agent'
TraceAI::Auto.init(provider: application_provider)
# Or exporter: application_exporter, or processor: application_processor.
```

An existing global SDK provider also qualifies. Explicit providers are reused, never
installed over another global provider. An exporter/processor without a provider gets
an agent-created SDK provider with ordinary OTel resource detection. Already-attached
host processors/exporters are not attached twice; `processor:` takes precedence over
`exporter:` when both are supplied. Host-owned destinations retain the original view.
An exporter newly attached by the agent receives detached S1-sanitized SDK records,
including hide controls; it uses the normal bounded batch processor.
No FI exporter is constructed in neutral mode. With explicit destinations, set
`fi_export: true` **and** a complete FI pair to add FI export alongside them;
otherwise FI credentials in the environment do not add an unexpected destination.
Without explicit destinations, a complete FI pair retains the existing FI-export default
(unless `fi_export: false`). Neither neutral initialization nor instrumentation tests
FI authentication or connects to an FI collector. Content capture stays ON by default.

Requiring `traceai/auto` schedules automatic initialization without activating gems.
In a Bundler project (an explicit `BUNDLE_GEMFILE` or a discovered `Gemfile`/`gems.rb`),
initialization waits for the application's `require 'bundler/setup'` or `Bundler.setup`.
Standalone scripts install dependency-free hooks when a supported transport is loaded,
and initialize at the first client request (before the official OpenAI SDK creates
its internal Fiber). Neither requires nor requests select additional gem versions:
JSON and OTLP protobuf serialization are internal, with no `json`, `securerandom`,
`base64`, protobuf or transport-gem require on an unresolved application's behalf.
If the application has already selected all SDK dependencies, the agent uses OTel;
otherwise an internal, bounded wire-only queue exports without activating gems.
Application JSON selection can follow even a health request or an AI request.
The loader observes class definitions/require completion; it never replaces
`Kernel#require` or resolves application constants, including autoloaded namespaces.

`init` configures once per process and returns that provider. Later calls (including
concurrent calls and calls with different options) return it unchanged and log one
line, once. There is no reconfiguration path. Set explicit options with the manual
require shown above, before the zero-code bootstrap initializes. A provider already
installed by the application is retained; its sampler remains authoritative. A new
agent-owned SDK provider uses a fixed parent-based ratio sampler.
`TraceAI::Auto.auto_instrument(instrumentations: ['wire', 'rack'])` can add available
integrations later; it never removes enabled integrations or changes the provider,
exporter, credentials, project or sampler. Disable-after-enable is unsupported.

`TraceAI::Auto.flush(timeout: 5)` drains agent-created processors and explicitly
borrowed neutral destinations. `.shutdown(timeout: 5)` shuts down only agent-created
processors; borrowed processors/providers are flushed, never shut down. With only a
host provider, flushing drains that provider's existing destinations. FI-only deployments
continue to flush just their agent processor, not unrelated host exporters. Both have
a shared maximum five-second caller wait and reject concurrent/reentrant flushes. Normal
exit flushes; SIGTERM flushes on a separate thread, then invokes the pre-existing
handler or restores and re-raises the default signal. SIGINT is never changed.
A handler the application installs later takes precedence; its normal exit still
runs the exit hook. SIGKILL and `exit!` cannot flush.

## Configuration

Precedence: explicit Ruby arguments, environment, `FI_CONFIG_FILE` (default
`./futureagi.toml`), defaults. The file accepts flat string/boolean/numeric keys
and single-line arrays of strings, ignores table headings, and understands
comments outside quoted strings. It is bounded to 64 KiB. `api_key` and `secret_key`
resolve atomically: the highest-precedence source with both nonempty strings wins.
A source supplying just one half is ignored, with one warning naming the missing
`FI_API_KEY` or `FI_SECRET_KEY`; halves from different sources are never combined.
All FI values read from every recognized source seed the S5 registry, including
ignored halves and lower-priority complete pairs; pair selection affects export
authentication, not whether a value is protected. Other settings retain their
independent precedence. The Ruby agent does not read fi profiles directly;
`fi run` supplies its resolved credentials through environment.

```toml
project_name = "checkout"
project_type = "observe"
instrumentations = ["openai", "wire"]
disabled_instrumentations = []
wire_capture = true
wire_max_body_bytes = 4194304
sample_rate = 1.0
debug = false
```

Supported agent variables: `FI_API_KEY`, `FI_SECRET_KEY`, `FI_BASE_URL`,
`FI_PROJECT_NAME`, `FI_PROJECT_TYPE`, `FI_PROJECT_VERSION_NAME`,
`FI_INSTRUMENTATIONS`, `FI_DISABLED_INSTRUMENTATIONS`, `FI_WIRE_CAPTURE`,
`FI_WIRE_MAX_BODY_BYTES`, `FI_SAMPLE_RATE`, `FI_CONFIG_FILE`, `FI_AUTO_DEBUG`.
The Ruby `fi_export:` option (flat TOML key `fi_export`) controls the optional FI destination.
`OTEL_SDK_DISABLED=true` disables the agent, including neutral destinations. Without
FI credentials, a preload waits dependency-free for an application SDK destination;
lazy hooks do not capture until a destination exists. With neither destination nor
a complete FI pair, initialization leaves the provider untouched and warns once
(at exit if no request/explicit initialization occurs). Preloads, including enabled preloads with credentials,
do not activate JSON, OpenTelemetry, or transport dependencies before an application's
Bundler setup; the flat config parser itself has no gem dependencies. The embedded preload shim
checks Ruby compatibility before loading Ruby-3-only code, so unsupported interpreters
warn once and still run the application. Invalid initialization fails open. `FI_AUTO_INSTRUMENT`
is Python-only; Ruby uses `RUBYOPT`.

Default project: `OTEL_SERVICE_NAME`, otherwise the working directory basename,
otherwise `DEFAULT_PROJECT_NAME`. Default type: `observe`. The OTLP/HTTP protobuf
exporter sends to `${FI_BASE_URL}/tracer/v1/traces` (base defaults to
`https://api.futureagi.com`) with `X-Api-Key` and `X-Secret-Key`. FI resource keys
match Python register: `project_name`, `project_type`, `project_version_name`
(default `DEFAULT_PROJECT_VERSION_NAME`), a per-initialization UUID
`project_version_id`, `eval_tags="[]"`, `metadata="{}"`, `semantic_convention="fi"`.
No network calls happen at initialization. Queues are bounded to 512 spans; one
export attempt is used rather than upstream unbounded `Retry-After` sleeping.
Unreachable collectors cannot change an application's result; queued spans may
be lost. A timed-out flush worker may finish in the background, never blocking
process exit beyond the caller deadline.

## Capture and transport coverage

The byte-identical shared catalog covers 25 wire formats, including chat,
Responses, embeddings, reranking, moderation, image generation, transcription
and speech. Endpoint/body validation avoids claiming unrelated application routes.
Unknown gateways need a valid body; unavailable/oversize bodies are captured only
on known provider hosts. AssemblyAI polling is captured only at completed/error.

* `Net::HTTP#request` is prepended. A writer tee inside the existing response read
  preserves the original request result, body destination, cached body, chunk
  objects, errors, and streaming callbacks. Buffered calls receive no invented
  response block, preserving native retries after premature closes. It does not read ahead. Arbitrary
  streaming request bodies are unavailable; already-resident native `StringIO`
  and current official SDK `ReadIOAdapter`/`StringIO` storage can be snapshotted
  without reading or rewinding. Multipart uploads are scanned while transmitted
  when metadata is not already available. Text fields are capped at 64 KiB, file
  parts are counted rather than retained, independently of upload size. Native
  File-backed multipart streams are teed at Net::HTTP's copy site (rather than
  overriding `File#read`, which `IO.copy_stream` bypasses); stream identity and
  starting position are preserved, with no read-ahead or rewind.
* Faraday's `net_http` adapter, `ruby-openai` 8.3.0, official `openai` 0.75.0 and
  official `anthropic` 2.3.0 use Net::HTTP in the installed source. Real client
  calls to local mock servers exercise these SDKs, not SDK-shaped stubs.
  `openai` and `ruby-openai` share `require 'openai'` and the `OpenAI` namespace;
  applications should choose one. The test bundle contains both and explicitly
  selects each gem's entrypoint in isolated child processes.
* `httpx` 1.9.0 is patched lazily after `require 'httpx'`, or immediately if already
  loaded. Per-request callbacks pair batched requests; a decoder tee covers both
  buffered responses and the `:stream` plugin without consuming the body.
  Header-first `:stream` iteration (`response.status`/`headers` before `each`)
  restores the caller's context inside HTTPX's private Enumerator Fiber, preserving
  suppression, deduplication, parents and context attributes. Consumer exits finalize
  captures even when that Fiber remains suspended. Multipart encoder metadata is
  read without reading file streams. Unknown custom body encoders are unavailable.
  Callbacks/capture references are cleaned up when the transport finishes.
  HTTPX retries retain one logical-request span with the final attempt's response;
  previous attempt bytes, errors, binary/truncation flags and completion state
  are discarded. A non-retried mid-stream error retains its partial output.
  The standard `:follow_redirects` plugin is observed at its actual outbound-request
  boundary, including an initially unrelated route redirecting to an AI endpoint.
  Each redirect chain has at most one wire span, with the final recognized route's
  request metadata and response, retaining the first recognized request's start and
  parent. Batched chains remain independent; buffered/streamed native responses
  and helpers are preserved. Earlier credentials remain in the process-wide privacy
  registry even when HTTPX strips Authorization on a cross-origin redirect. The agent never
  follows redirects or changes redirect headers itself.
* RubyLLM 2.1.1 uses Faraday's default `net_http` adapter. A real Chat Completions
  client call is included in the local suite. Its default OpenAI protocol is now
  Responses; that wire format is exercised independently by shared fixtures.
  The lazy HTTPX hook observes class-load completion via a one-shot TracePoint,
  not a `Kernel#require` wrapper, preserving Zeitwerk/Rails implicit namespaces.
* Non-default Faraday adapters (Typhoeus, Excon, HTTP.rb), direct sockets, WebSocket
  realtime APIs, gRPC, custom native clients and custom HTTPX transport plugins
  are not covered. HTTPX upgrades/private internals and alternate Net::HTTP
  implementations need compatibility tests. Roast is not installed/tested.

Library spans marked `gen_ai.span.kind=LLM|EMBEDDING|RERANKER|GUARDRAIL` and their
covered descendants suppress wire capture, even through intermediate SDK-native
spans. Coverage lives on the span, not in an unbounded global ancestry map;
framework `CHAIN|AGENT|TOOL` spans by themselves do not suppress capture. OpenTelemetry
`untraced` suppression and in-flight context guards prevent export loops and
nested captures. Parenting/start timestamps are saved at transport request time,
then the CLIENT span is created only on completion. For official OpenAI 0.75.0,
a request-scoped context bridge captures the caller at the SDK requester's `execute`
boundary and attaches it inside the SDK's Enumerator Fiber. Buffered and streamed
calls retain suppression, deduplication, framework parents, and session/user/metadata,
including when stream consumption occurs after the caller's scope exits. Other
custom Fibers/threads must propagate their own context. Anthropic 2.3.0 propagates context and emits
its own vendor span without traceAI's kind marker. That vendor span is preserved;
a wire child is added only when no covered library ancestor already records the call.

There are no dedicated traceAI Ruby library packages in this gem. Such installed
libraries are reported as `missing_instrumentor`, not falsely `instrumented`;
the enabled wire transports still cover known protocols. Optional installed
`opentelemetry-instrumentation-rack` enables Rack server transaction spans. That
optional path is not exercised in this bundle. No other server/framework root
instrumentation is claimed. Library-layer enablement beyond Rack remains future
work rather than an invented compatibility matrix.

## Privacy and safety

S6 recognition is loaded, without activating gems, from the shipped byte-identical
`lib/traceai/auto/privacy.json` catalog. One credential-name predicate serves
transport header/query observation, parsed-body harvesting, captured attribute/body
keys and returned/free-text URL queries. Credential ancestry survives flattened and
bracket-indexed paths; catalog exclusions preserve token limits, usage counters,
key identifiers and logprob token text. One media predicate covers magic-byte
base64 under any key, exact data-URI values, scoped MIME/type siblings and audio
payloads of any size, including numeric samples. Only a bounded base64 prefix is
decoded for magic detection. Metadata/transcripts reset inherited media context;
query pairs share indexed hints without collapsing duplicate hints or changing
unchanged URL spelling, ordering, fragments or prose. The S1 detached export
snapshot and S5 process-wide literal registry remain authoritative. Content capture
is still ON by default.

`test/test_round6_privacy.rb#test_s6_every_spec_privacy_fixture` executes every
shipped privacy fixture, including its second-pass stability check. The adjacent
catalog/fixture byte-identity test prevents local list drift; update the spec and
re-sync the copies rather than adding per-site name lists. Session-named fields,
including `session.id`, are redacted by the catalog even when used as application
correlation metadata; user IDs and ordinary metadata are unaffected unless their
names/values independently meet a privacy rule.

Never put raw audio or embedding vectors in span attributes. Binary responses
are counted, not buffered. Other request/response capture buffers default to
4 MiB, configurable up to a hard safety ceiling of 64 MiB; exceeding the limit
sets `traceai.wire.truncated` and stops body parsing. Multipart file bytes never
enter the metadata buffer. Gzip/zlib/raw-deflate decoding is incremental and bounded
by expanded size. Unsupported encodings, including `br`/`zstd` in this bundle,
retain request/status attributes and set `traceai.wire.undecoded_encoding`.
Mid-stream transport errors preserve partial parsed output and ERROR status;
early consumer close/non-local exit or a consumer-raised exception preserves partial
output and marks incomplete, not ERROR. A fully delivered unencoded Content-Length
response is complete even when the consumer exits non-locally before callback unwinding;
transfer-encoded or decoded bodies are not compared to an incompatible byte count.
Classification uses the consumer boundary, not exception classes; consumer `IOError`
and `Timeout::Error` remain consumer aborts.
No retries, headers, response reads or extra network calls are added to
application requests.

Every wire string is scrubbed before content hiding: inline data URLs (base64,
percent-encoded or literal payloads, including media-type parameters); base64 in
named media containers and MIME-identified media objects, including short audio
and flattened/indexed paths such as `input.audio.0`, `input.inlineData.data`,
`input.source.data` with sibling `input.source.media_type`, and
`input.attachment.0.data` with sibling `input.attachment.0.mimeType`. MIME recognition
stays scoped to its own container/index and preserves transcript/MIME metadata;
long base64 in otherwise unqualified `data`/`bytes` fields; credential JSON keys at
every depth; signed URL parameters; request credential header values and configured
FI credentials of eight or more characters echoed into content/errors. Tool argument/JSON-string
attributes are structurally scrubbed too. Malformed/truncated embedded JSON and
codec-limit failures are sanitizer failures, not a reason to export the original
string. The final snapshot fails closed, retaining identity/timing and numeric
status rather than leaking unexamined content. Earlier wire scrubbing clears the
affected data on failure so it neither drops the entire wire span nor exposes
unsafe wire content to another processor.
Credentials never appear in doctor configuration. Collector diagnostics do not
print raw error bodies/keys.

`FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`,
`FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`,
`FI_HIDE_INPUT_IMAGES`, `FI_HIDE_EMBEDDING_VECTORS`,
`FI_HIDE_LLM_INVOCATION_PARAMETERS`, and `FI_HIDE_ERROR_DETAILS` are supported.
Content hiding uses `__REDACTED__`; message-side hide flags drop flattened messages;
raw aliases are also redacted to prevent bypasses. Invocation-parameter hiding
alone drops the `gen_ai.request.*` family (including nested/new parameter aliases),
except model/stream identity metadata, plus native `anthropic.request.*` (except
request ID) and the `anthropic.thinking` family. This covers reasoning effort,
service tier, enabled thinking and thinking budget as well as sampling/token
parameters. It also redacts the raw `input.value` alias, which contains invocation
configuration; flattened input messages remain available unless their own hide
flag is enabled. Input hiding applies to TTS transcripts; output hiding applies to
STT transcripts, including every dotted `gen_ai.audio.transcript.*` descendant on
span, event and other sanitized attribute surfaces. Error details are hidden by
default when any input/output content-hide flag is on. `FI_PII_REDACTION` is not
implemented. Existing external instrumentors control their own capture behavior;
the FI exporter additionally scrubs configured FI secrets and applies hide flags
to its copies, without mutating records sent to the application's other exporters.
Transport entry hooks observe credential headers (including AWS
`x-amz-security-token`) and credential/signed-URL query values independently of
wire-span creation. Net::HTTP also observes finalized request headers after native
proxy authentication is added, plus the actual HTTPS proxy CONNECT header block
at the native write boundary (including an explicit `start` before `request`).
HTTPX observes the final protocol header enumeration after its proxy plugin adds
authentication, for both HTTP requests and HTTPS CONNECT tunnels. These observers
do not generate credentials, change native headers or consume request bodies.
Covered library/native ancestors, OTel suppression
and nested-capture guards do not turn observation off. When the SDK
forwards native/application spans, observation also continues with
`FI_WIRE_CAPTURE=false`, a disabled `wire`/transport integration or an allowlist
excluding wire. Suppressed calls never inspect or buffer bodies. Credential-named
body fields enter the registry only when the enabled wire layer already parses them.

One thread-safe process-wide registry holds at most 1,024 literal values of eight
or more characters. Reobserving a value refreshes its recency; least-recently-observed
values are evicted when full. Initialization seeds configured FI keys and environment
values whose names contain `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL`
or `AUTH` (case-insensitive). No credential groups, parent references or inherited
secret lists are attached to spans. The export sanitizer uses an immutable,
longest-first snapshot for every span, including unrelated traces and spans started
or queued before discovery. Already exported telemetry cannot be changed retroactively.
Registry access errors fail closed to identity/timing-only snapshots. Registry values
are never included in agent diagnostics or logs; another exporter's originals remain
unchanged.

Integrations can register a credential discovered outside a supported transport:

```ruby
TraceAI::Auto.register_secret(provider_credential)
```

The method copies a native string, returns `true` when registered, and ignores
non-string or shorter values (`false`), without invoking application stringifiers.
It is available before lazy initialization and does not enable capture or modify
requests. Content capture remains **on by default**; use the hide controls above
to suppress input/output content rather than relying on literal-secret matching.
One authoritative sanitizer snapshots every FI export immediately before OTLP
serialization: application/library/wire attributes, arrays, names, status text,
events, links (including linked tracestate), resource attributes, and scope
name/version/attributes. This includes native JSON-valued `gen_ai.input.messages`,
`gen_ai.output.messages` and `gen_ai.system_instructions` aliases, input/output
attribute families, tool definitions and image/file URI aliases. NaN and infinities
become strings. Unknown values become their native type name without calling
application getters/stringifiers. A per-span traversal/byte/depth budget bounds
cycles and branching structures. Any sanitizer failure drops span content and
unprotected metadata while retaining protected counters, structural identity and
timing; independently bounded sanitization recovers the safe span name and resource
identity (including separate FI routing), never unsanitized values. Other spans still export.
Other exporters never see those changes. Transport start is recorded before agent
matching/copying, and the end is recorded when bytes or a transport error are
observed, before wire parsing and before consumer callbacks unwind.

Optional Ruby context helpers scope wire attributes on the current Fiber:
`TraceAI::Auto.using_session(id) { ... }`, `.using_user(id) { ... }`,
`.using_metadata(hash) { ... }`, or `.with_attributes(hash) { ... }`.
These are Ruby-native helpers, not cross-language context propagation. When disabled
or awaiting Bundler setup, they execute the application block unchanged without loading JSON.

## Doctor and bootstrap

```sh
traceai-auto doctor              # masked JSON report plus bounded diagnostic export
traceai-auto doctor --no-export  # offline report, no collector call
traceai-auto bootstrap           # inventory and installation advice; no auto-install
```

The report distinguishes instrumented, pending, not_installed,
missing_instrumentor, disabled, and failed integration states. `manual` is reserved
for future integrations that need application code. Startup debug reports do not
make network calls and say `collector: not_tested`; doctor explicitly tests export.
The diagnostic uses an isolated always-sampled provider connected only to the FI
processor, so `FI_SAMPLE_RATE=0`, an unsampled parent or an existing always-off
application provider do not prevent the connectivity check. It neither changes
application sampling nor exports the diagnostic to the application's other exporters.
The bootstrap command does not install packages; review/install optional packages
with Bundler yourself. Agent configuration can disable `wire` or individual
`net_http`/`httpx` adapters. Integration allowlists select `wire` as a layer; its
transport adapters need not be listed separately.

## SDK attribute budgets (A7)

Wire attributes are sanitized and deterministically projected before SDK insertion,
using the actual provider's `span_limits.attribute_count_limit` (default 128).
Standalone capture honors `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` / `OTEL_ATTRIBUTE_COUNT_LIMIT`
with the same default. The agent never raises a host's limit. Identity, outcome,
provider/model/operation, usage, provenance and degradation markers reserve capacity
before content. Output wins over input; message/tool groups and MIME companions are
atomic. Input messages retain a contiguous newest suffix with original indices, and
aggregate input cannot restore an evicted older conversation. Optional metadata comes last.

`traceai.capture.dropped_content_count` reserves its own slot and counts omitted content
attribute keys, accumulating existing counts. The distinct OTel all-attribute drop
count includes pre-admission projection loss; snapshots preserve recorded attribute,
event and link totals, and OTLP serializes their dropped counts. When protected fields
alone exceed capacity, no content is admitted: degradation/drop markers, identity,
outcome, provider/model/operation, usage and provenance retain a deterministic lexical
prefix within each family. Zero-slot impossibility is counted out of band as
`report[:projection_zero_limit_spans]` rather than increasing the host's limit.

## Usage and streaming observations (A8/A10)

Raw cumulative usage snapshots merge field by field (including explicit zero/null),
then normalize once. Anthropic/Bedrock input includes separate cache read/write
counts; Gemini input includes tool-use prompt tokens and output includes thoughts.
OpenAI cache/reasoning details remain included, not added twice. Known input plus
output determines total; a differing provider total is retained separately. Missing
base counters and explicit nulls remain unknown rather than becoming zero.

`gen_ai.response.time_to_first_chunk` observes the first nonempty transport body
chunk. `gen_ai.response.time_to_first_text` observes the earliest complete frame
with text, refusal, reasoning, model-generated tool name/arguments/input/code, or
audio/transcript output. Role/ID/usage/finish events and empty scaffolds do not
qualify. These are arrival observations, never tool execution or audio playback.
The two native transport hooks sample monotonic time before decoding; deferred
parsing uses the recorded byte-offset/arrival pairs. SSE requires a blank-line
delimiter, NDJSON requires its newline, and JSON-array members complete at their
own closing brace, even if the array is interrupted later. Split UTF-8 and
coalesced frames retain their actual completing chunk's timestamp. The same
observed complete frames determine usage and milestones after failures.

## Verification and limits

`test/test_wire.rb` recursively discovers shared `fixtures/**/*.json` at test time,
including Round 6. At 2026-10-31 the contract has 340 fixtures: 330 wire cases and
10 synthetic decoded Bedrock cases. The latter run separately in
`test/test_round6_milestones.rb`, not through artificial SSE wire requests. The
harness implements compact UTF-8 byte partitions, injected arrival offsets,
failure-after-chunk prefixes, gzip and every assertion form. The milestone tests
also cover native decoder timing, bounded timestamp storage, delayed parsing and
agent-on/off Net::HTTP/HTTPX byte/error parity, including gzip. A8 projection
regressions run in `test/test_round6_tokens.rb`. The packaged catalog is compared
byte-for-byte with the shared specification.
`test/test_safety.rb` covers additional formats, privacy, config precedence,
unavailable bodies, encoding expansion bounds and incremental multipart parsing.
`test/test_e2e.rb` spawns plain applications with `RUBYOPT=-rtraceai/auto`, decodes
real protobuf exports at a local collector, checks SDK calls, streaming passthrough,
existing providers/exporters, dedup/suppression/parenting, missing credentials,
SIGTERM chaining/default exit, and bounded flushing/retry behavior.
`test/test_regressions.rb` additionally checks unsupported system Ruby 2.6 preload,
a default-JSON-pinned application initializing Bundler after a disabled preload
(with and without a config file), native File multipart uploads against known and
compatible hosts, protocol failures versus consumer errors, HTTPX retries after
JSON/binary/truncated/error responses, and diagnostics with zero/always-off sampling.
`test/test_round2.rb` covers native Net::HTTP block/retry semantics, enabled JSON 2.6.3
pinning before explicit/default/manual Bundler setup, dependency-free disabled helpers,
real official OpenAI Fiber context (buffered and streamed), consumer exception identity
and incomplete status in both transports, Cohere v2 SSE, and Gemini message-part versus
stream-chunk separators (including shared fixture 18).
`test/test_round3.rb` adds standalone late JSON/Bundler selection and first-request
capture, HTTPX header-first context and consumer-abort matrices, actual Anthropic
content-mode export hiding without mutation, Cohere v2 streamed tools/v1 NDJSON,
all streaming endpoint variants, Responses text-part separators, and full versus
partial Content-Length consumer exits against baseline applications.

`test/test_round4_contract.rb` adds no-agent baselines for standalone health/AI
requests followed by late JSON selection, real Zeitwerk namespace timing, original
response identity/helpers and never-consumed HTTPX streams. It also checks concurrent
once-only initialization, additive integration enablement, real Anthropic native-span
ancestry, every export surface, non-finite/unknown values, fail-closed cycles and
branching budgets, invocation aliases, and transport timing with deliberately slow
validation, parsing and consumer callbacks.
`test/test_round4_findings.rb` adds the independent verifier's regressions: incomplete
tool JSON with byte/EOFError baseline parity, embedded JSON parser/depth failures,
native Anthropic credentials with wire/transport capture disabled, header-first
HTTPX credential discovery without wire capture, native/nested invocation aliases
across export surfaces, and redirected AI routes with buffered/streamed identity,
request-body parity, batching, deduplication, nested guards and callback cleanup.
Cross-origin redirect tests check stripped auth and credential echoes both with
OTel and in the dependency-free standalone pipeline; other exporters remain unchanged.
`test/test_round5.rb` covers bounded/LRU/thread-safe process-wide secret registration,
FI/environment seeding, public registration, all export surfaces and registry-failure
snapshots. Real 401 echoes through covered Net::HTTP/Anthropic and HTTPX calls are
compared with no-agent error baselines, then checked on application exception spans,
pre-existing/queued spans and unrelated later traces. Suppressed hooks must harvest
headers/query values without touching bodies; parsed nested body fields must also
protect later unrelated spans.
`test/test_round6_followup.rb` covers transcript-family descendants on their STT/TTS
hide side and safe resource/name recovery after malformed span content, including
real loopback OTLP and no-agent comparisons of the host exporter's original view.
Recovery also tests registry failure, malformed identity and independent field budgets.
Protected-key regressions cover numeric, boolean, string, array and non-finite values,
longest-first credential replacement, and literal-schema-only recovery without the
registry. A real loopback Net::HTTP request seeds an observed token before normal and
malformed-content application spans; decoded FI OTLP scrubs both that token and the
FI key from attribute names, while agent-on/off host records and response bytes match.
Real Net::HTTP gzip tests cover inflater discovery before and after initialization,
with controlled raw arrivals and a five-second decoder-clock advance; application
bytes remain identical with the agent off/on. Anthropic NDJSON and SSE regressions
check response identity, inclusive usage, repeated snapshots and incomplete-line
exclusion, not just milestone detection.

## Known limitations (updated 2026-10-31)

- (2026-10-31) Identity recovery still fails closed if the name or resource itself
  cannot be sanitized within its budget, or the known-secret registry is unavailable:
  the affected field is empty, never passed through unsanitized. Malformed span
  content no longer erases an independently safe name, application service/custom
  resource identity or separate FI routing. Recognized resource content is dropped
  during fallback; other exporters retain the application's original data.
  If the registry is unavailable, fallback also omits application-defined usage keys:
  a protected prefix cannot establish that the suffix is credential-free. Only
  numeric/boolean values under fixed schema keys are recoverable in that state.
  With a working registry, fallback scrubs protected keys as well as their values;
  this is not an exception permitting credential disclosure.
- (2026-10-31) Arrival association is bounded to 4,096 nonempty retained chunks
  per response, independently of the body byte cap. After that bound the span
  is marked `traceai.wire.incomplete`; an already known first-text milestone is
  kept, but output beyond the associated prefix cannot establish one. Empty
  transport chunks never consume an arrival slot. Storage is cleared on finish
  and retry. Post-hoc decompression without native per-chunk association omits
  first-text and marks the stream incomplete rather than substituting parse time.
- (2026-10-31) The ten decoded Bedrock recognizer fixtures do not qualify AWS
  binary event-stream decoding: actual Bedrock binary streams remain
  request/status-only. Native gzip timing is tested for Net::HTTP and HTTPX;
  untested third-party decoder implementations remain outside that support claim.
- (2026-10-30) Extremely small host attribute limits cannot hold more protected
  fields than their capacity: A7 emits no content and retains only the documented
  protected-family prefix (an eight-slot host cannot retain every usage counter).
  Zero-slot hosts cannot retain any attribute marker; the out-of-band counter
  reports this. Application/library-owned spans still obey their own SDK admission
  policy; the wire projection cannot recover attributes they discarded earlier.
- (2026-10-30) Neutral SDK destinations require the application's own selected
  OpenTelemetry SDK/dependencies before first agent initialization; the standalone
  dependency-free pipeline remains FI wire-only. Host-owned processors/exporters
  keep their original data and lifecycle; the agent does not retrofit their privacy
  policy or replace their destination. Newly agent-attached exporter routes receive
  the S1 boundary. Disable-after-enable/reconfiguration remains unsupported.
- (2026-10-30) S6 is structural recognition, not general PII detection. Its broad
  credential markers intentionally redact session-named correlation fields and
  unlisted names containing credential patterns; exclusions must be changed in
  the shared catalog, not locally. Media encoded/transformed into an unrecognized
  form can remain ordinary text. Use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`,
  `FI_HIDE_INPUT_IMAGES` and the granular hide controls when content must never
  leave the process; the catalog does not disable capture by default.
- Local toolchain is Homebrew Ruby 3.2.2 on macOS arm64. Ruby 3.1, JRuby,
  TruffleRuby, Windows, remote provider/collector acceptance and production-scale
  load are not verified. Local conformance is not proof of complete two-layer
  parity or a "10x" performance claim.
- No dedicated Ruby traceAI library instrumentors ship here. Optional Rack and
  compressed third-party HTTPX plugins are not exercised. Non-default Faraday
  adapters, custom native transports, gRPC and WebSocket realtime remain outside
  coverage; transport private-API upgrades need compatibility testing.
- An embedded standalone process that has not selected the OTel SDK's dependencies
  uses the bounded wire-only queue (512 records, 64-record batches, five-second
  scheduling). It does not replace an application provider installed later. Select
  the SDK before first agent initialization to forward application/library spans;
  reconfiguration/disable-after-enable is intentionally unsupported.
- To avoid activating a different OpenSSL/digest version, HTTPS collector exports
  require the application's TLS implementation to have loaded already. Normal
  HTTPS provider calls do this. An HTTP-only standalone application with a HTTPS
  collector must load its chosen OpenSSL first or use an explicitly configured
  trusted HTTP collector. Otherwise export fails open with a warning; records may
  be lost. No TLS verification bypass or hidden dependency activation is used.
- Brotli/zstd remain undecoded unless supported by the transport; unsupported body
  encodings keep request/status metadata. Privacy traversal limits can deliberately
  replace an unusually large/deep span with identity/timing only. The internal JSON
  codec bounds nesting/node count, numeric literals (1,024 characters) and positive
  exponents (308); exotic bodies beyond those bounds may be uncaptured. Custom
  Fibers and threads must propagate their own OTel context.
- JSON-looking strings beginning with `{` or `[` must be valid, bounded JSON to
  survive export sanitization. Partial tool calls and even ordinary bracket-prefixed
  text that is not valid JSON conservatively clear the wire/FI snapshot's content
  and unprotected metadata, retaining structural identity/timing/status and safely
  scrubbed protected fields. Numeric usage/drop counters survive an exhausted text
  budget. The application's data and other exporters' native/application spans
  remain unchanged.
- (2026-10-30) Literal-secret matching cannot infer application transformations such
  as extracting a signed-URL signature into plain text, JSON encoded inside a
  string, numeric re-typing, base64 or splitting. Credentials that never cross a
  supported instrumented transport and have no credential marker may be unknown.
  Use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` and the more granular hide controls above;
  integrations may register additional untransformed strings with
  `TraceAI::Auto.register_secret(value)`.
- (2026-10-30) Manual/one-line instrumentation without the agent's transport hooks
  discovers only initialized FI/environment credentials (plus explicit registrations).
  Environment credentials added after initialization require explicit registration.
  Registry capacity is 1,024 values; an evicted value is no longer protected by
  literal matching unless reobserved/registered. Suppressed requests do not parse
  bodies, and already exported spans cannot be retroactively scrubbed. Use the hide
  controls above for content that must never be exported.
- SIGKILL/`exit!`, queue overflow and unreachable collectors can lose records.
  PII classification/redaction beyond the documented credential/media/hide rules is
  not implemented.

Local review host-safe commands (caches/install/temp data stay under `.bundle`,
gitignored; the outer helper is specific to this shared review host):

```sh
bash /Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh /Volumes/nikm4/futureagi/traceai-auto/ruby/script/heavy /opt/homebrew/opt/ruby/bin/bundle install --jobs 2 --retry 2
bash /Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh /Volumes/nikm4/futureagi/traceai-auto/ruby/script/heavy /opt/homebrew/opt/ruby/bin/bundle exec rake test
```

The outer helper takes one of the shared host's two heavy slots. The Ruby wrapper
also checks free-memory/load gates and holds the Ruby `heavy.lock` through the
command. Both release their locks on failure and preserve the command's exit code.
