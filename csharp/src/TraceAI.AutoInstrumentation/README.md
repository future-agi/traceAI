# traceAI .NET auto-instrumentation

A fail-open .NET 8 LTS / .NET 10 startup hook with two capture layers: built-in GenAI
`ActivitySource` telemetry and protocol-aware `HttpClient` capture. This is an
initial agent, not a CLR profiler: coverage depends on the transport and library
behaviors described below. It does not capture every possible AI call.

## Install and run without changing application code

Build from the repository root with a .NET 10 SDK; the application needs only its
own runtime, not a .NET 10 installation. Publish **one framework per directory**:

```sh
# .NET 8 application (no application package/code changes required)
dotnet publish csharp/src/TraceAI.AutoInstrumentation/TraceAI.AutoInstrumentation.csproj \
  -c Release -f net8.0 -o csharp/artifacts/auto/net8.0
export FI_API_KEY='...'
export FI_SECRET_KEY='...'
export DOTNET_STARTUP_HOOKS="$PWD/csharp/artifacts/auto/net8.0/TraceAI.AutoInstrumentation.dll"
dotnet /absolute/path/to/YourNet8Application.dll

# For .NET 10, publish with -f net10.0 -o csharp/artifacts/auto/net10.0,
# then point DOTNET_STARTUP_HOOKS at that directory's DLL instead.
```

### Supported runtime matrix

| Application runtime | Hook payload | Shared DiagnosticSource | Verification / limits |
| --- | --- | --- | --- |
| .NET 8 LTS | `net8.0` | Runtime 8.x, or application-provided 9.x/10.x | Offline real-hook tests on .NET 8.0.33/macOS arm64 cover stock 8.x and app-provided 9.0.0; all wire/startup modes, OpenAI 2.8.0 and ASP.NET Core 8. |
| .NET 10 | `net10.0` | Runtime 10.x | Same real-hook matrix on .NET 10.0.14/macOS arm64, OpenAI 2.8.0 and ASP.NET Core 10. |
| .NET 9, .NET Framework, .NET 6/7 or earlier | None supported | — | Not part of the supported/tested matrix. |
| NativeAOT, trimmed/single-file deployments, browser/WASM | None verified | — | Not supported by this verification; do not assume startup-hook availability. |

Payloads are managed code with no native profiler. Windows/Linux and architectures
other than macOS arm64 have not been exercised here. Use the matching payload;
**a `net10.0` hook cannot load in a .NET 8 process**. DiagnosticSource majors outside
8–10 fail open with a warning rather than creating conflicting Activity types.
Application-provided DiagnosticSource 10 on .NET 8 is accepted but not a separately
exercised combination. The hook never upgrades the app's chosen DiagnosticSource.

Keep **all** files in the published agent directory together, not just the hook
DLL. Attach to the application process, not `dotnet build`, `dotnet test`, or
`dotnet run` (which can attach it to build/test infrastructure as well). On Windows
set the same environment variables in PowerShell. Multiple startup hooks are
separated by the OS path separator (`:` on Unix; `;` on Windows).

For a local NuGet installation, package the dependencies and agent:

```sh
dotnet pack csharp/src/FIInstrumentation -c Release -o csharp/artifacts/packages
dotnet pack csharp/src/TraceAI.AutoInstrumentation/Net8Sdk/TraceAI.AutoInstrumentation.Net8Sdk.csproj \
  -c Release -o csharp/artifacts/packages
dotnet pack csharp/src/TraceAI.AutoInstrumentation/AgentRuntime/TraceAI.AutoInstrumentation.Runtime.csproj \
  -c Release -o csharp/artifacts/packages
dotnet pack csharp/src/TraceAI.AutoInstrumentation -c Release -o csharp/artifacts/packages
```

Add that directory as a package source and reference `TraceAI.AutoInstrumentation`
version `0.1.0`. These packages have not been published by this change. The .NET 8
support package compiles the unchanged shared FI SDK sources for .NET 8; the legacy
SDK project remains net10-only. The .NET 10 target keeps its existing SDK reference.
For version-conflict isolation prefer the separate published hook directory rather
than mixing agent dependencies into an application's own output.

The alternative **in-process** code entry point is (choose one entry point so
lifecycle/diagnostic APIs run in the context that owns the agent):

```csharp
using TraceAI.AutoInstrumentation;
TraceAIAuto.Init();
```

`Init(options => { ... })` overrides environment/file settings; the nullable
properties in `AutoOptions` expose credentials, collector URL, project, integration
filters, body limit, sampling, debug logging, config path, `TraceConfig`, and an
optional custom OTel `BaseExporter<Activity>` or caller-owned `TracerProvider`.
Initialization is **once per process**.
Subsequent calls return without executing the configuration callback, rereading
configuration, or rewiring listeners; one diagnostic line explains this. This also
applies after a failed/disabled first initialization and after `Shutdown()`.
Changing settings or restarting the agent requires a new process. Disabling an
already enabled integration is unsupported. A BCL-only process-wide guard also
prevents duplicate providers if a manual entry point is accidentally loaded next
to the isolated hook. A non-owning load context does not control the other context's
flush/shutdown/doctor APIs. Repeated bootstrap attempts do not reload the payload,
including after a load failure. Custom exporters remain caller-owned and are never
shut down or disposed by the agent. Supplying `AutoOptions.Exporter` or
`AutoOptions.Provider` activates the agent without FI credentials; only the default
FI collector requires the complete FI pair. Neutral initialization does not validate
or connect to an unused FI collector URL. An invalid `FI_PROJECT_TYPE` (including
its code/file equivalents) falls back to `observe` with one warning for supplied
destinations, rather than disabling instrumentation; default FI collector setup
still rejects an invalid project type. `OTEL_SDK_DISABLED=true` still disables
agent activation, leaving an already-running host provider alone.

A supplied provider is reused, not replaced or disposed. Configure its source
subscriptions **before** building it (`AddSource("TraceAI.AutoInstrumentation")` for
wire spans, plus desired native sources such as `"OpenAI.*"`). Its sampler, resource,
processors and original export views remain host-owned. `Init(o => o.Provider = host)`
only enables capture hooks and bounded flushes; it does not add an FI exporter even
when FI credentials exist. An explicitly supplied `Exporter` alongside that provider
adds only that detached/privacy-protected destination, with separate project routing
on its resource snapshot. Shutdown disables agent hooks and its added destination,
flushes the host within the deadline, and never shuts down the host provider.

## Configuration

Precedence is **code > environment > file > defaults**. The config file is
`./futureagi.toml`, or `FI_CONFIG_FILE`/`AutoOptions.ConfigFile`. The supported flat
TOML subset is strings, booleans, numbers and single-line arrays of strings.
Comments outside strings and table-heading lines are ignored; this is not a full
TOML parser. Malformed lines are ignored. Keep credentials out of committed files.
`FI_API_KEY` and `FI_SECRET_KEY` resolve as one atomic pair: explicit arguments,
then environment, then file. An incomplete or blank pair is ignored as a whole
with one warning naming the source and missing half; halves never mix across sources.

```toml
project_name = "checkout-agent"
project_type = "observe"
instrumentations = ["openai", "semantic_kernel", "aspnetcore", "wire"]
disabled_instrumentations = []
wire_capture = true
wire_max_body_bytes = 4194304
sample_rate = 0.25
debug = false
```

| Environment | Default / meaning |
| --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | Complete pair required for the default collector; optional with a supplied exporter or provider. |
| `FI_BASE_URL` | `https://api.futureagi.com`; OTLP HTTP/protobuf at `/tracer/v1/traces`. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, working directory name, then `DEFAULT_PROJECT_NAME`. |
| `FI_PROJECT_TYPE` | `observe`; alternatively `experiment`. |
| `FI_CONFIG_FILE` | `./futureagi.toml`. |
| `FI_INSTRUMENTATIONS` | All integrations; otherwise a comma-separated allowlist. |
| `FI_DISABLED_INSTRUMENTATIONS` | Empty; denylist takes priority. `wire` disables HTTP capture. |
| `FI_WIRE_CAPTURE` | `true`. |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304` (4 MiB), separately for request/response capture. |
| `FI_SAMPLE_RATE` | `1`; parent-based trace-id ratio sampling, clamped to 0–1. |
| `FI_AUTO_DEBUG` | `false`; print masked configuration/integration diagnostics at startup. |
| `OTEL_SDK_DISABLED` | `true` disables the agent entirely, without missing-key warnings. |

All `FI_HIDE_*` switches from `FIInstrumentation.TraceConfig` are honored in this
path, including flattened message attributes and audio transcript text. Their
lower-case file equivalents work too (`hide_inputs = true`, etc.). Agent-created
wire attributes are masked before Activity creation and attribute projection, even
with only a supplied provider: its listeners/exporters never receive hidden wire
content. Detached export sanitization remains a second pass on agent destinations;
host-owned application/library spans keep their original host-export view.
Invocation parameters obey `FI_HIDE_LLM_INVOCATION_PARAMETERS`. Native library
`gen_ai.system_instructions` and `gen_ai.tool.call.arguments` obey input/input-text/
input-message hiding; `gen_ai.tool.call.result` obeys the corresponding output
flags. Native `gen_ai.user.message`, `gen_ai.system.message` and `gen_ai.tool.message`
events are input; `gen_ai.assistant.message` and `gen_ai.choice` events are output.
Their content/message/tool fields obey that side's input/output, message and text
hide flags without renaming exported event attributes. The `input.*`, `output.*`, `gen_ai.input.*` and `gen_ai.output.*` families
obey their side's hide flags, including `.text` and flattened message-part `.text`
attributes under the text-hide flags, on spans/events/links/resources/scopes.
Image hiding includes Anthropic `type: image` blocks (including remote
`source.url`), OpenAI image properties, and Gemini `fileData`/`file_data` image
parts and file URIs, element-wise in string/object arrays. Code can provide
`AutoOptions.TraceConfig` without changing existing SDK defaults.

Credentials are used only for collector authentication. Provider authorization
headers are never exported; known provider keys are redacted if echoed in a wire
response/error. Credential-named JSON keys at any depth, signed-URL query values,
and credential echoes (including configured FI keys) are replaced by `<redacted>`.
Inline base64 media is replaced by `<omitted: N base64 chars>` before TraceConfig
masking, including percent-escaped and mixed escaped/plain data-URL payloads
(without decoding media bytes). Other RFC 2397 data URIs, including percent-encoded
or literal audio, become `<omitted: data URI>`. Explicit audio payload fields and
nested/flattened/indexed audio data/bytes are protected at every size. A shared
path-aware rule also protects MIME-tagged audio/image `data`/`bytes` and indexed
descendants (for example `parts.1.inlineData.data.0` with sibling
`parts.1.inlineData.mimeType`) in JSON and on all attribute surfaces, independent
of key order and payload size. Transcript/format metadata remains available.
Side-specific hide flags also cover
audio transcripts and derived content.
Raw audio/binary response bodies are counted, never retained by the agent.
Embedding response vectors are never captured, including nested `embedding`/
`embeddings` containers in generic Bedrock InvokeModel response JSON, regardless
of `FI_HIDE_EMBEDDING_VECTORS`.

**One authoritative export-time privacy boundary** builds detached snapshots just
before serialization: attributes of every supported value type, status descriptions,
event names/attributes (including exceptions), link attributes/tracestate, span
tracestate, resource attributes, and scope name/version/tags/schema URL. It never
redacts the shared application Activity in place. Immutable event/link data is
copied and sanitized, not used as a reason to drop an otherwise valid span. Arrays
are copied element-wise. Non-finite floats become the strings `NaN`, `Infinity`
and `-Infinity`, preserving their spans and the rest of the batch.

`JsonElement`/`JsonNode` traversal copies only known framework values. Unsupported
objects (including objects wrapped with `JsonValue.Create`) become their type name;
raw byte arrays become `<redacted>`. User-defined `ToString()`, getters, serializers,
equality and enumerable implementations are never used to sanitize unknown objects.
Ambient FI metadata is read from the known SDK AsyncLocal fields and copied using
the same closed-value rules, rather than invoking the SDK's general serializer on
an HttpClient callback. Transport errors read only the BCL's stored exception text,
never a virtual `Message` override. Unrecognized host context layouts are skipped, fail-open.
**One process-wide known-secret registry** is shared as BCL state across the isolated
hook and default load contexts. It holds at most 1,024 values of eight or more
characters, evicting the least recently registered/observed value first. At
initialization it learns every supplied FI key (including ignored incomplete explicit
pairs and overridden TOML/environment values) and credential-like environment values
(names containing KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL or AUTH).
`TraceAIAuto.RegisterSecret(value)` lets an integration register additional values;
shorter values are ignored. The export boundary takes one immutable, longest-first
snapshot per batch and replaces these values in every exported string, including
unrelated and later spans. There is no per-request secret set, span-start inheritance
or ancestor credential propagation. Registry values never appear in diagnostics.

The HttpClient observer always collects credentials, before wire matching, sampling,
model-span de-duplication, suppression or `FI_WIRE_CAPTURE=false` can skip capture.
Header values retain their individual boundaries with HTTP whitespace trimmed;
credential/signed-URL query values are registered in encoded and decoded forms.
URL discovery and export scrubbing share one credential-query vocabulary, including
`client_secret`, `authorization_token`, `password`, `key` and `api_key`. Every recognized
credential header also drives export attribute-key masking (including indexed keys).
The observer inspects only exact, already-buffered BCL `StringContent`,
`ByteArrayContent`, `ReadOnlyMemoryContent`, `FormUrlEncodedContent` and safe multipart
text parts. It does not materialize HttpContent buffers, serialize custom content,
read one-shot streams, wrap suppressed bodies or touch multipart file payloads.
Buffered gzip, deflate/zlib and Brotli are decoded within bounded work before
credential-field discovery; incomplete/unsupported decoding fails closed rather
than accepting a prefix. This also covers nested MCP `authorization_token` fields.
The real-send multipart parser publishes each parsed text credential immediately,
including one-shot text parts and duplicate fields, before any response or span
completion; it never reads streaming bodies ahead on suppressed requests.
Discovery/registry failure disables content export process-wide while preserving
span identity/timing; application HTTP bytes, exceptions and content remain unchanged.
The fully merged resource includes `OTEL_RESOURCE_ATTRIBUTES`, detected attributes
and all project fields, with the same hide and credential rules. Project routing is
separate (`project_name`); it never replaces detected `service.name`,
`service.version` or `service.instance.id`, or synthesizes application identity from
the instrumentation's version. All supplied FI values are refreshed before export,
so ordinary LRU churn cannot evict ignored/overridden FI credentials.

Attribute projection follows the shared A7 contract, independent of insertion order.
Wire candidates are projected before Activity creation; forwarded Activities are
sanitized and mapped on a detached snapshot before the SDK's bounded serializer
(Activity itself has no bounded tag container on DiagnosticSource 8–10). Reserve
identity, outcome, provider/model/operation, all known usage, provenance and
degradation fields, including one dropped-content marker when necessary. Atomic
output message/tool groups and output value/MIME pairs precede input messages,
newest numeric index first. Retained input is a contiguous newest suffix; a missing
newer group prevents retaining older ones or an aggregate input that restores them.
Output omitted for capacity prevents admitting input. MIME/type context groups stay
atomic, and payloads are scrubbed before selection. Optional metadata comes last.
`traceai.capture.dropped_content_count` counts omitted content keys (including MIME,
parameters and message fields), accumulates prior projection loss, and survives S1.
It is distinct from OTLP's `dropped_attributes_count`, which continues to count only
SDK serializer eviction, not pre-projection loss. Native exporter resolved limit
options are respected; otherwise `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` overrides
`OTEL_ATTRIBUTE_COUNT_LIMIT` (default 128). If protected fields themselves overflow,
no content is admitted: degradation, identity, outcome, provider/model/operation,
usage and provenance families retain their lexical prefix. A zero limit produces a
bounded one-time diagnostic rather than increasing the host's limit. Host Activities,
resources and other exporters' views are unchanged.

Value traversal has a shared 4,096-node/4-MiB text budget and a 64-level depth bound;
cycles, expanding graphs and oversized values redact the affected attribute.
The export snapshot additionally bounds the combined attribute/event/link/scope
work to 4,096 nodes/8 MiB per span, 65,536 nodes/8 MiB per batch and 4,096 nodes/4 MiB
per resource. Credential observation is capped at 4 MiB (also respecting the configured
body limit), eight encoding layers, 128 distinct values per discovery and 4,096
characters per value. Registry capacity is 1,024; discovery/value-budget failures
fail closed, while capacity overflow uses LRU eviction. A snapshot failure discards
all content but retains identity, flags and timing. Snapshot shells use the tested
DiagnosticSource 8–10 private content fields to preserve IDs without invoking
ActivitySource constructors or application listeners. An unrecognized layout omits
telemetry rather than reverting to the unsafe shared Activity. These privacy rules
apply to the agent's exporter, not an application's separately configured exporters.
The agent does not enable libraries' sensitive-content diagnostics automatically.

## Library layer and transactions

| Integration name | Sources / behavior |
| --- | --- |
| `openai` | `OpenAI.*`; enables `OpenAI.Experimental.EnableOpenTelemetry`. |
| `extensions_ai` | `Experimental.Microsoft.Extensions.AI*`, `Microsoft.Extensions.AI*`; the app must use `OpenTelemetryChatClient`/`UseOpenTelemetry()`. |
| `semantic_kernel` | `Microsoft.SemanticKernel*`; enables `Microsoft.SemanticKernel.Experimental.GenAI.EnableOTelDiagnostics`, not the sensitive-content switch. |
| `azure_inference` | `Azure.AI.Inference*`; enables `Azure.Experimental.EnableActivitySource`. |
| `azure_openai` | `Azure.AI.OpenAI*`; modern clients also emit `OpenAI.*` (enable `openai` too). |
| `agents` | `Experimental.Microsoft.Agents.AI*`, `Microsoft.Agents.AI*`; the framework must opt in to its OpenTelemetry wrapper. |
| `aspnetcore` | `Microsoft.AspNetCore*`; listens to built-in incoming SERVER activities; preserves remote parents. |
| `wire` | `HttpHandlerDiagnosticListener` / `HttpRequestOut`. |

Listeners cover sources created after startup. `gen_ai.system` and legacy token
usage attributes are mapped to current conventions, and operation names map to
`gen_ai.span.kind` (`LLM`, `EMBEDDING`, `AGENT`, `TOOL`, etc.). No blanket outgoing
HTTP instrumentation is installed. Framework AGENT/CHAIN/TOOL spans allow child
wire capture; model/embedding/reranker/guardrail spans suppress it through the
entire ancestor chain, even with intermediate SDK-native client/retry spans. The
agent marks covered model activities and passively tracks native descendants,
including explicit parent contexts where `Activity.Parent` is null and model kind
tags set after Start. The passive listener requests no sampling and exports no
unrelated source spans. OTel and SDK suppression scopes and nested captures are respected.

Native SDK telemetry is experimental and operation/version coverage varies.
Sources/defaults were checked against these upstream references:

- [OpenAI .NET observability](https://github.com/openai/openai-dotnet/blob/main/docs/Observability.md)
- [Semantic Kernel telemetry](https://learn.microsoft.com/en-us/semantic-kernel/concepts/enterprise-readiness/observability/telemetry-with-console)
- [Azure AI Inference diagnostics](https://github.com/Azure/azure-sdk-for-net/blob/main/sdk/ai/Azure.AI.Inference/README.md)
- [Microsoft framework source defaults](https://github.com/microsoft/opentelemetry-distro-dotnet/blob/main/docs/azure-monitor-getting-started.md)
- [Agent framework telemetry wrapper](https://github.com/microsoft/agent-framework/blob/main/dotnet/src/Microsoft.Agents.AI/OpenTelemetryAgent.cs)

## Wire behavior and coverage limits

The embedded `Wire/wire-protocols.json` is a byte-identical copy of the shared
catalog. The independent parser implements all 25 catalog formats, including
chat/completions/Responses, embeddings, rerank, moderation, image JSON, STT/TTS,
Anthropic, Gemini, Cohere, Bedrock, Ollama, Deepgram, ElevenLabs, AssemblyAI and
Cartesia. Matching validates request bodies to avoid claiming application routes.
The tests read **every shared fixture at test time**, not a copied fixture set.

Request capture reads only exact buffered `StringContent`, `ByteArrayContent` and
`ReadOnlyMemoryContent` types, plus safely buffered multipart text fields. It
records bounded multipart file descriptors and sizes. Multipart serialization is
observed during the real send, so one-shot text fields (64 KiB each) and unknown
file sizes are captured without read-ahead; file payloads are never retained.
`JsonContent`, custom/derived content and `StreamContent` are not eagerly read or
serialized: user serializers/streams may have side effects. Consequently ordinary
JSON protocol requests in those content types are captured without request content
only on catalog provider hosts (`traceai.wire.body_unavailable=true`). Unknown
proxy hosts still require a validated body. Binary audio request sizes are
available when Content-Length is known. Endpoint paths/query fields can supply
model/voice identifiers, but do not bypass required-body validation.

At HttpClient Stop, the original response and `HttpContent` are retained. For the
standard SocketsHttpHandler framework response content, the agent taps its owned
stream via the tested `_stream` field; it never calls a custom content factory or
reads an unread body. Read-stream and content serialization/copy paths forward
original bytes, cancellation and errors. Custom/unknown response content is left
untouched, including its subclass helpers, rather than replaced by a generic wrapper.
The application owns all reads. A bounded copy of text/JSON/SSE/NDJSON is parsed at
completion; beyond the limit it is discarded and `traceai.wire.truncated=true` is
recorded. Binary content is only counted. No streamed chunks are read ahead.
The framework-owned stream itself is a delegating tap, so its concrete type is not
preserved; repeated content stream retrieval still returns the same object.

Spans are created at body completion with explicit request start and end times.
Start is captured at the diagnostic transport boundary before matching/copying;
end is the last observed response byte (or transport error), frozen before response
parsing or Activity callbacks. Empty/unread bodies use the observed headers boundary
when no byte was read. Parsing and delayed disposal do not inflate transport duration.
The span retains the request-time parent/context and suppression decision, even when the reader later
enters a distinct host OTel or SDK suppression scope. Completion restores the
reader's ambient state unchanged. Disposal before EOF produces an OK partial-output
span with `traceai.wire.incomplete=true`, unless the observed byte count exactly
matches a known Content-Length. Observing that final byte completes the span
immediately, including binary/truncated bodies: neither an extra EOF read nor
disposal is required before flush or normal exit. A body that is neither fully read
nor disposed cannot report completion. Streaming endpoint variants
(`:streamGenerateContent`, `:streamRawPredict`, `converse-stream`, and
`invoke-with-response-stream`) carry the stream flag independently of framing,
including ordinary JSON, Gemini JSON arrays and Bedrock binary event streams.
Measured `gen_ai.response.time_to_first_chunk` is attached when body bytes were
actually observed; empty responses never synthesize a measurement. Independently,
`gen_ai.response.time_to_first_text` marks the chunk that completed the first
nonempty text/reasoning/tool-name/tool-argument/generated-audio event. Role-only,
empty scaffolds and usage-only events do not count; whitespace output does.
Bounded byte-offset/monotonic-arrival checkpoints are recorded before capture work;
framing/recognition happens only after completion, never using parse-time clocks.
Split events use their completing chunk, coalesced events share a timestamp, and
failed/incomplete trailing frames never produce usage or first-text observations.
Gemini JSON-array streams retain complete members before an interrupted outer array,
merging only complete usage snapshots and timestamping each member's closing byte.
SSE `event:` names are retained when the JSON has no explicit event type. Both SSE
blank-line and NDJSON newline delimiters must arrive before a frame contributes
output or usage, including at EOF. JSON objects use their closing byte's arrival.
Recognition includes refusal and audio-transcript deltas, legacy function calls,
Mistral nested thinking/text lists, Responses MCP/custom/code-interpreter output,
Anthropic hosted tool names, Gemini executable code, and Cohere v1 tool-call chunks
and v2 thinking. Tool results, scaffold metadata and empty payloads do not qualify.
Bedrock's ten synthetic SSE fixtures run in a decoded-event recognizer harness;
they do not claim support for AWS binary event-stream transport decoding.
SSE/NDJSON responses also carry
stream attributes on non-streaming endpoint paths. Transport/read errors produce ERROR spans and retain
parsed partial output. Gzip, deflate and Brotli bodies are decoded within the body
limit; unavailable decoders are reported in `traceai.wire.undecoded_encoding`.
Application destination-write failures during response copying are consumer aborts
(OK/incomplete), not provider errors; genuine source/read failures remain ERROR.
Reading/copying errors still reach the application unchanged. SSE/NDJSON parsing
is bounded and happens only after completion; there
is no per-token export. Bedrock binary event streams are request/status only.
JSON/SSE base64-audio variants are size-counted without allocating decoded audio.

No CLR profiler, native SDK interposition, custom ActivitySource discovery,
WebSocket/gRPC AI transport, metrics capture or automatic NuGet installation is
provided. `HttpClient` subclasses/handlers that do not emit normal diagnostics are
not covered. Frameworks requiring explicit wrappers are reported as `manual`, not
falsely as instrumented.

## Diagnostics and lifecycle

These code APIs apply to the in-process entry point. The isolated zero-code hook
reports on `FI_AUTO_DEBUG=true` and performs its bounded shutdown on ProcessExit.
On Unix, it also joins the runtime's SIGTERM dispatcher for a bounded flush.

```csharp
Console.WriteLine(TraceAIAuto.Doctor());             // no network
Console.WriteLine(TraceAIAuto.Doctor(testExport: true)); // explicit test span + flush
Console.WriteLine(TraceAIAuto.Bootstrap());          // installed-library/coverage report
var report = TraceAIAuto.GetStatus();
TraceAIAuto.RegisterSecret(providerCredential);     // shared across load contexts
TraceAIAuto.Flush();
TraceAIAuto.Shutdown();
```

Statuses use `instrumented`, `pending`, `not_installed`, `disabled`, or `manual` in
this built-in-source implementation; initialization failure reports `failed`.
`missing_instrumentor` is reserved for integrations requiring separate
instrumentors; no such packages are currently required here. Discovery examines loaded assemblies and app dependency
manifests. `instrumented` means the listener attached to a matching source, not a
guarantee the library emits every operation. `Bootstrap()` is read-only; adding a
NuGet package requires rebuilding the host application.

Doctor and startup debug reports mask imported `FI_` configuration whose names
contain KEY, TOKEN, PASSWORD, SECRET, CREDENTIAL or AUTHORIZATION, including
unrelated application client secrets. Doctor reports the collector state:
`not_tested`, `accepted`, `failed`, or `custom_exporter`. `accepted` means the last
OTLP export got a successful transport result, not that data was indexed. Sampling
can suppress a doctor test span. Collector failures warn once and drop telemetry;
there are no network calls at startup. Flush on normal `ProcessExit` is bounded.
On Unix, SIGTERM additionally flushes completed queued spans with a 3-second deadline
through `PosixSignalRegistration`, including on .NET 10 where the default SIGTERM
need not raise `ProcessExit`. It never sets signal cancellation or replaces/re-raises
the signal: the runtime retains its default termination action/exit code and runs
application handlers. If the application cancels SIGTERM, capture stays enabled
(the callback flushes, rather than shutting down). No SIGINT handler is installed;
SIGKILL and other abrupt termination can still lose data.
`Flush(timeoutMilliseconds)` and `Shutdown(timeoutMilliseconds)` bound the caller's
wait even if a custom exporter blocks; reentrant exporter callbacks never wait on
themselves. Flush success requires the actual exporter calls for pre-flush submissions
to have returned, not just an SDK queue acknowledgement. It does not guarantee
collector indexing or retry failed exports. Queue loss prevents a success claim
for that agent instance. A misbehaving exporter may continue on a background thread,
never on the application's shutdown thread.

### Assembly loading and existing OTel providers

The global, internal `StartupHook` bootstrap assembly contains only framework types,
so application DI/plugin scanners can safely call `GetTypes()` on it. It loads
`TraceAI.AutoInstrumentation.Runtime.dll`, the FI SDK, patched OpenTelemetry **1.15.3** and supporting
Microsoft.Extensions dependencies in a private `traceai-auto` AssemblyLoadContext.
They do not bind to or replace the application's OTel assemblies. The sole shared
DiagnosticSource assembly is resolved through the default context, so HttpClient,
application spans, and agent listeners have identical Activity/DiagnosticListener
types. No default-context resolver that hijacks application dependencies is installed.
Host suppression and FI context attributes are read across the boundary as framework
values, without passing incompatible OTel types between load contexts.

OpenTelemetry 1.15.3's OTLP serializer calls APIs absent from DiagnosticSource 8/9.
On those versions a bounded (16 MiB/message) compatibility transport serializes the
stable OTLP HTTP/protobuf trace schema: resource, scope name/version, span IDs,
parentage, timestamps, typed attributes, events, links and status. It uses the same
final privacy boundary, authentication, endpoint and timeout, without downgrading
to vulnerable OTel versions. DiagnosticSource 10 uses the upstream exporter.
The compatibility transport includes sanitized scope tags/schema URLs when those
APIs exist, but does not synthesize the span's remote-parent flag on older
DiagnosticSource versions. Compatibility export performs no
disk retry. Manual in-process initialization has no private dependency boundary;
normal NuGet dependency resolution applies there.

The real-hook coexistence probe deliberately ships OTel 1.11.2 and DiagnosticSource
9.0.0 in the application, asserts that its provider/exporter continue working,
checks host suppression and parentage, scans the bootstrap types, and verifies that only one
DiagnosticSource exists. OTel 1.11.2 is a **test-only conflict fixture**, not a
recommended application version. Arbitrary OTel versions/integrations are not all
certified. Invalid hook paths/CLR-level loading failures occur before Initialize
and cannot be caught by the agent.

.NET uses additive ActivitySource listeners rather than one replaceable global
provider. By default the agent builds a side-by-side provider/exporter. With
`AutoOptions.Provider`, it reuses the caller's provider and adds only explicitly
supplied destinations; neither mode disposes or replaces a host provider. Existing
host exporters keep working; overlapping
host source subscriptions may intentionally export the same activity to their
own destinations. Parent-based sampling is rechecked at the agent export boundary
so another listener recording a shared activity does not override its sampling
policy; the host's exporters are unaffected.

## Tests

```sh
dotnet build csharp/FIInstrumentation.sln -c Release
dotnet test csharp/FIInstrumentation.sln -c Release
```

Tests use no live provider or Future AGI credentials. The net10 test runner builds
both framework payloads and launches actual .NET 8 and .NET 10 child processes.
Stock console probes have **no package or telemetry references**; richer probes
exercise ASP.NET Core, OpenAI and an older host OTel provider. All send HTTP to a
local mock provider; a local collector decodes the actual OTLP protobuf. The suite
reads every current shared fixture (including Gemini JSON-array streams, multipart
messages and Cohere v2 SSE); fixture files are never copied or changed by tests.
Synchronous processor chains and exporter entry/release events make privacy and
blocked-flush regressions deterministic, without sleeps. Independent-verifier
regressions additionally exercise environment resource privacy through stock .NET
8/10 startup-hook processes, branching cycles/shared DAGs through Activity.Stop,
gated user-defined ToString overrides at start/stop/export and HTTP de-duplication,
and streaming/non-streaming endpoint variants across five response framings.
`RoundThreeRegressionTests` adds gated getter-safety checks at stop/export for
mutable tags, immutable events/links and custom JSON values, HTTP credential OWS,
actual OTLP native-content masking, Anthropic/OpenAI image URLs, nested Bedrock
vectors, diagnostic client-secret masking, application destination-write aborts,
and request-time suppression preserved across deferred completion. The older-host
coexistence probe also consumes an accepted response under the host's separate
suppression scope on both runtimes. Compatibility protobuf
is checked against the patched upstream exporter for resource/attribute values,
timestamps, events, links and error status, plus its message-size bound. Cases cover JSON,
SSE byte preservation, async/default-buffered/synchronous reads, de-duplication,
missing credentials, disabled SDK, sampling (including overlapping host listeners),
masking, binary counting, truncation, idle-startup/no-network behavior and dead
collectors. A real ASP.NET Core host verifies SERVER-to-wire parentage, and an
OpenAI .NET 2.8.0 client uses the mock server to verify native library telemetry.
The separate legacy `csharp/e2e_tests` console program calls a live provider and is
not part of this offline verification.

`RoundFourPrivacyTests` covers concurrent enrichment after snapshotting through
actual OTLP, every privacy surface through native and compatibility transports,
non-finite scalars/arrays, string-array and Gemini file-backed images, ambient
getter safety and fail-closed snapshot budgets. `RoundFourTransportTests` compares
fresh no-hook and hooked processes on .NET 8/10 for buffered/synchronous/streaming,
zero-byte/canceled/partial/unread bodies and content/response helper identity;
it also checks real-hook scope/tracestate/event/link privacy, intermediate native
span de-duplication, custom content preservation and transport timestamps before
parsing/Activity callbacks. `RoundFourBlockerTests` covers query credential error
echoes through actual .NET 8/10 hook exports (wire, deduplicated library and wire-off
privacy-only paths), every recognized auth header on all export surfaces without
request discovery, and SIGTERM against no-hook baselines. Signal cases check both
default exit 143 and repeated application-canceled signals followed by new spans.
`RoundFourSecondBlockerTests` adds actual OTLP checks for `.text` hide families on
every privacy surface, percent-escaped/mixed data URLs, AWS session tokens and MCP
body-credential error echoes under covered native descendants (wire/library/wire-off
paths). Real-hook .NET 8/10 exact Content-Length probes compare no-agent bytes and
export before any EOF read/disposal; an in-process assertion checks flush with both
response and stream still open. Credential budget failures and custom-content
serializer/length safety have dedicated regressions.
`RoundFiveTests` covers directional GenAI message events on both actual OTLP
transports, shared URL/header credential vocabularies, a bounded/thread-safe LRU,
longest-first immutable snapshots, public registration across private/default load
contexts, environment seeding, all export surfaces and unrelated/later spans.
Actual .NET 8/10 startup-hook probes compare compressed request behavior with
no-hook baselines for wire/library/privacy-only paths, verify default-context public
registration against the isolated exporter, and exercise message-hide flags.
Incomplete gzip/Brotli/deflate/zlib, decoded-size limits, safe buffered BCL content
shapes, diagnostic echoes and indexed/short MIME-tagged media have regressions.
`RoundFiveBlockerTests` checks multipart part-header and serialized-length stability,
byte-for-byte sync/async .NET 8/10 upload parity, process-wide registration from
observed streaming multipart fields (including duplicate/nested credentials), and
short audio/non-base64 data URIs on every actual OTLP privacy surface. Real-hook
probes verify later unrelated spans and native library media on both runtimes.
`RoundFiveMediaPathTests` adds the independent verifier's exact flattened MIME/audio
JSON and indexed image-attribute reproductions. It checks both actual OTLP
transports across span/event/link/resource/scope surfaces, status/event names,
JSON DOM/array values, case-insensitive MIME aliases, indexed MIME values,
order independence, segment-boundary controls and original-Activity isolation.
.NET 8/10 startup-hook cases compare flattened/indexed media exports and application
output with no-hook baselines.
Unit lifecycle tests explicitly simulate fresh processes using test-only reflection;
production has no reset or reconfiguration API.

## Shared privacy catalog (S6)

`Wire/privacy.json` and `Wire/privacy-fixtures.json` ship byte-identical to
`spec/auto-instrumentation/`; `sync.py` checks both. The runtime embeds the catalog,
so startup-hook deployment does not depend on a source checkout or an external
privacy file. `PrivacyCatalog.CredentialName` is the one normalized, path-aware
credential predicate, shared by transport headers/query/body harvesting and every
export surface. Query names additionally use the catalog's signed-URL parameters.
Credential ancestry persists through containers and indices; metric exclusions and
logprob-token context apply only to their component, not independent credentials.

`PrivacyCatalog.MediaString` is the one media predicate. It recognizes bounded
decoded magic prefixes under any key, exact whole-value data URIs, all catalog
MIME aliases/siblings and indexed parents, explicit audio payloads of any size,
and long unrecognized base64 under catalog fields. Payload ancestry crosses
wrappers but resets at audio metadata boundaries. Numeric audio payloads are
omitted; transcripts, voice, format and usage stay intact unless another privacy
rule or hide control applies. Free-text URL scanning is distinct from exact URL
and data-URI framing; query pairs preserve spelling, order, duplicates, fragments
and unrelated prose. Duplicate MIME/type hints combine, never overwrite.

`EverySpecPrivacyFixturePassesProductionBoundaryAndIsIdempotent` runs every shared
privacy case through production scrubbing and repeats it for idempotence. Actual
OTLP tests cover both exporter routes, resources, scopes, events, links and span
attributes, plus .NET 8/10 no-hook parity and suppressed-call secret harvesting.

`RoundSixBlockerTests` exercises ignored/overridden FI credentials after LRU churn,
public no-credential initialization, independent application resource identity,
actual native/compatibility OTLP limits (128, 8 and 0), dropped counts, snapshot
isolation and gated real-HttpClient streaming milestones. `WireFixtureTests`
recurses through every shared fixture directory, including all Round-6 fixtures,
and replays exact chunk sizes, arrival offsets and mid-frame failures.

Current wire fixtures also apply A8 inclusive token algebra once after merging
raw cumulative snapshots. Anthropic/Bedrock input includes cache reads/writes;
Gemini adds tool-use input and thoughts output. OpenAI details are not added twice.
Known input/output produce their sum; a differing raw total remains in
`gen_ai.usage.provider_total_tokens`. Unknown and explicit-null counters are not
invented as zero; absent optional additions use provider-schema defaults.

## Known limitations

- **2026-10-31 — Supplied-provider subscriptions:** .NET providers fix source
  subscriptions at Build. Configure `TraceAI.AutoInstrumentation` and desired
  native library sources on the caller's builder before supplying that provider;
  the agent does not reflectively rewrite its subscriptions or sampler. A
  standalone processor can be installed on that provider before initialization;
  there is no direct `AutoOptions.Processor` convenience entry point.
- **2026-10-31 — Host-listener sampling (S6 verifier OTHER):** a host listener can
  record a root rejected by the agent's ratio sampler; a child may then inherit
  that host-recorded parent and pass the parent-based sampler, exporting an orphan
  child even at agent sample rate zero. This is a sampling-policy limitation, not
  whole-provider sampling control; host exporters remain unaffected.
- **2026-10-31 — Attribute limits:** no implementation can retain protected fields
  beyond a host limit smaller than that protected set. Such limits deliberately
  drop content and retain the A7 family/lexical prefix; zero emits no attributes
  and warns once. OTel's SDK eviction count and the projection's accumulated
  content-key loss count describe different stages and are not conflated.

- **2026-10-31 — Concatenated gzip members (round-six verifier OTHER):** credential
  discovery's strict gzip trailer validation compares the combined decoded output
  with the last member's trailer. Valid concatenated-member requests can therefore
  conservatively suppress that request's captured content, even though the BCL/app
  reads them successfully. Discovery failure no longer poisons the process-wide
  registry or unrelated later spans. Request bytes and application behavior are
  unchanged. Prefer a single gzip member, or use the `FI_HIDE_*` controls when
  captured content is unnecessary. Multi-member strict decoding is not fixed here.
- **2026-10-31 — Streaming milestone bounds / encoded and binary streams (R6a verifier OTHER):**
  arrival tracking retains at most 4,096 nonempty transport reads per response.
  First-output events beyond those checkpoints, discarded/truncated content,
  unsupported/binary framing or still-encoded compressed bodies have no first-text
  timestamp; no estimate is synthesized from parsing time. On the standard
  SocketsHttpHandler diagnostics path, capture can see encoded bytes even with
  `AutomaticDecompression` enabled. Bounded decompression can recover text/usage,
  but does not associate decoded events with transport arrival timestamps. Such
  streams, and Bedrock binary event-stream responses, can conservatively carry
  `traceai.wire.incomplete=true` even when the application reads every byte and
  the response status is OK. This marker is not proof of an early application
  close. First-chunk timing remains available independently; application bytes
  are unaffected, and generated audio is not claimed to be played audio. This
  timestamp-association/marker limitation is not fixed here.
- **2026-10-30 — S6 recognition trade-offs:** catalog credential patterns also mask
  application `session.id` and names containing unexcluded `sig`/`key`/`auth`
  components. These are shared recognition semantics, not per-SDK exceptions.
  Use non-credential identifiers for telemetry that must remain visible. Unknown
  or transformed secrets/media outside structural catalog rules still require the
  `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, message/text/image and parameter hide controls
  or explicit `TraceAIAuto.RegisterSecret` registration.
- **2026-10-30 — fi profile resolution:** this agent reads explicit arguments,
  environment and its documented flat TOML subset; it does not directly read fi
  profiles. Use fi's resolved complete environment pair or a complete TOML pair.

- **2026-10-30 — S5 privacy scope:** application-transformed secrets (plain extracted
  signatures, JSON encoded inside another string, numeric re-typing, base64 or
  splitting) and unmarked credentials that never crossed an instrumented transport
  are not guaranteed value-based redaction. Use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`,
  `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES` and the text/parameter hide
  controls for sensitive content, or explicitly call `TraceAIAuto.RegisterSecret`.
  Manual instrumentation without the agent's HttpClient observer has only FI keys,
  initialization-time environment credentials and explicitly registered values.
- **2026-10-31 — S5 bounds/unsupported buffered layouts:** only the documented exact
  BCL buffered request types are inspected; custom/streaming/JsonContent serializers
  are never invoked for discovery. The 1,024-value registry evicts LRU values, so
  echoes of an evicted credential need hide controls or re-registration. Request
  body discovery uses the shared catalog's 16,384-node / depth-32 limits. Discovery
  overflow, unsupported/incomplete compression or an observed credential over 4,096
  characters suppresses only that request's wire content and marks it incomplete;
  discovered in-budget credentials remain registered, and later spans/resources
  are unaffected. A credential beyond the inspection budget cannot be learned for
  later unmarked echoes: use the `FI_HIDE_*` controls or explicit registration for
  sensitive content. Export-time sanitizer failures retain safe protected semantic
  fields and sanitized resource identity; if registry access itself is unavailable,
  unsafe identity strings are redacted rather than copied. Image hiding uses the
  same catalog traversal limits and fails closed per attribute, not by dropping
  the whole wire span.
- **2026-10-30 — TOML eval_tags (round-three C7):** string arrays in the flat TOML
  reader are comma-joined, so `eval_tags = ["first,with-comma", "second"]` loses its
  JSON element boundaries. Use `AutoOptions.EvalTags` or a JSON-encoded
  `FI_EVAL_TAGS` environment string when boundaries matter. This is not fixed here.
- **2026-10-30 — Runtime layout/stream identity:** privacy snapshots and standard
  HTTP stream taps depend on the verified DiagnosticSource 8–10 and
  SocketsHttpHandler managed layouts. Changed layouts fail open for the application
  and fail closed for export; coverage can be lost. NativeAOT/trimmed/single-file
  deployments and unsupported runtimes are not certified. Framework stream concrete
  types are not preserved (there is no CLR profiler/interposition); applications
  casting those private runtime stream types should disable `wire`. Native-ancestry
  tracking is bounded to 65,536 context keys and 256 parent hops; pathological
  graphs or libraries that never stop activities can exceed these coverage limits.
  A passive listener can make `ActivitySource.HasListeners()` true even though it
  requests no sampling for unrelated sources.
- **2026-10-30 — Custom response transports:** custom HttpContent and subclass stream
  helpers are deliberately untouched. Their bodies are not captured unless the
  normal SocketsHttpHandler framework stream is available. WebSocket/gRPC and
  handlers bypassing normal HttpClient diagnostics remain outside wire coverage.
- **2026-10-30 — Recoverable response-read failures (round-four/round-five verifier OTHER):**
  an already-canceled read or invalid-argument read finalizes capture as ERROR.
  If the application catches it and subsequently consumes the response successfully,
  the application still receives its original HTTP status/bytes, but telemetry can
  remain ERROR with missing output. Capture does not resume after that first failure.
  This telemetry inaccuracy is not fixed here.
- **2026-10-30 — Multipart body limit (round-four/round-five verifier OTHER):** multipart text
  capture uses a separate 64-KiB allowance per field (up to 128 fields), not the
  configured aggregate `FI_WIRE_MAX_BODY_BYTES`. Consequently a 1,024-byte prompt
  can be captured with that option set to 128 or zero without marking truncation.
  File bodies are still counted, never retained; export privacy/hide flags still
  apply. This limit mismatch is not fixed here. Disable `wire` or hide inputs if
  multipart text must not be retained.
- **2026-10-30 — Completion/termination:** a body neither read nor disposed cannot
  report completion. SIGKILL/abrupt termination or a host that exits immediately
  inside its own signal callback can lose queued telemetry. Unix SIGTERM flushing
  does not take over application signal handling; flush/shutdown remain bounded.
  If runtime signal registration is unavailable, the agent warns and retains only
  normal-exit flushing.
- **2026-10-30 — Platform coverage:** verification here is macOS arm64/.NET 8.0.33
  and 10.0.14, including older-host telemetry isolation. Other OS/architecture
  combinations need their own certification. Compatibility OTLP on older
  DiagnosticSource versions does not synthesize remote-parent span flags.
