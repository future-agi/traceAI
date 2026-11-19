# traceAI .NET auto-instrumentation

A fail-open .NET 8 LTS / .NET 10 startup hook with two capture layers: built-in GenAI
`ActivitySource` telemetry and protocol-aware `HttpClient` capture. This is an
initial agent, not a CLR profiler: coverage depends on the transport and library
behaviors described below. It does not capture every possible AI call.

## Install and run without changing application code

One published directory serves both .NET 8 and .NET 10 applications. The
application needs only its own runtime.

### Option 1: install the NuGet package

The package is first available on NuGet with the first NuGet release, which waits for
the gated release process. Until it is published, `dotnet add package` cannot find it:
use Option 2. The package is versioned together with FIInstrumentation (the
`fi-instrumentation-otel` package): the hook embeds the SDK sources, so both are
released at one version number.

Add the package to the application project (or to any scratch project, to only fill
the NuGet cache):

```sh
dotnet add package TraceAI.AutoInstrumentation
```

The package contains no library to reference. It holds the hook directory under
`tools/net8.0/any/`, and NuGet unpacks it into its global packages folder, with the
package id in lower case: `~/.nuget/packages/traceai.autoinstrumentation/<version>/`
(on Windows, `%USERPROFILE%\.nuget\packages\...`; set `NUGET_PACKAGES` to move it).
Point `DOTNET_STARTUP_HOOKS` at the hook DLL in that folder:

```sh
FI_API_KEY='...' FI_SECRET_KEY='...' \
DOTNET_STARTUP_HOOKS="$HOME/.nuget/packages/traceai.autoinstrumentation/<version>/tools/net8.0/any/TraceAI.AutoInstrumentation.dll" \
  dotnet /absolute/path/to/YourApplication.dll
```

Keep every file in `tools/net8.0/any/` together. Deploy that folder with the
application, or restore the package where the application runs.

### Option 2: build from source

Build from the repository root with a .NET 10 SDK:

```sh
dotnet publish csharp/src/TraceAI.AutoInstrumentation/TraceAI.AutoInstrumentation.csproj \
  -c Release -o csharp/artifacts/auto
FI_API_KEY='...' FI_SECRET_KEY='...' \
DOTNET_STARTUP_HOOKS="$PWD/csharp/artifacts/auto/TraceAI.AutoInstrumentation.dll" \
  dotnet /absolute/path/to/YourApplication.dll
```

Set the variables on the application's command line, as above, not with `export` in
your shell: an exported `DOTNET_STARTUP_HOOKS` also attaches the hook to later
`dotnet build`, `dotnet test` or `dotnet run` commands. Child processes started by the
application inherit the variable, and with it the hook, unless the application
removes it from their environment.

Tracing starts only when you set `DOTNET_STARTUP_HOOKS` (or call `TraceAIAuto.Init()`).
Once on, every installed AI integration below is traced with content captured by
default; the `FI_HIDE_*` switches hide it. Future AGI receives only the AI spans,
masked by the shared privacy catalog, and the incoming web request spans of requests
that made an AI call (set `FI_DISABLED_INSTRUMENTATIONS=aspnetcore` to send none).
The application's own OpenTelemetry provider keeps its sampled flag, its exports and
its downstream trace propagation unchanged, and the hook writes no tags to the
application's activities. On sources the application also follows (the
`Microsoft.AspNetCore` web request source, and AI sources it subscribes to), the
hook votes record-only, so those activities are created with
`IsAllDataRequested` set even when the application's sampler did not sample them:
the application's processors and instrumentation callbacks then also run for those
requests, while its exporters still skip them. The hook also sets these process-wide
library switches when the matching integration is enabled, after it has started
successfully:
`OpenAI.Experimental.EnableOpenTelemetry`,
`Microsoft.SemanticKernel.Experimental.GenAI.EnableOTelDiagnostics`, and
`Azure.Experimental.EnableActivitySource` (which turns on activity sources in every
Azure SDK client, not only AI clients).

### Supported runtimes

| Application runtime | Shared DiagnosticSource | Notes |
| --- | --- | --- |
| .NET 8 LTS | Runtime 8.x, or application-provided 9.x/10.x | Tested with real child processes, including a stock console application and an application that ships an older OpenTelemetry. |
| .NET 10 | Runtime 10.x | Same tests. |
| .NET 9, .NET Framework, .NET 6/7 or earlier | — | Not supported or tested. |
| NativeAOT, trimmed/single-file deployments, browser/WASM | — | Not supported; do not assume startup-hook availability. |

The payload is managed code built for `net8.0`, with no native profiler; it runs in
.NET 8 and .NET 10 processes. Continuous integration runs the tests on macOS and
Linux. DiagnosticSource majors outside 8–11 fail open with a warning rather than
creating conflicting Activity types. The hook never upgrades the app's chosen
DiagnosticSource.

Keep **all** files in the published agent directory together, not just the hook
DLL. On Windows set the same environment variables in PowerShell. Multiple startup
hooks are separated by the OS path separator (`:` on Unix; `;` on Windows). The hook
compiles the unchanged shared FI SDK sources for .NET 8 against its own pinned
OpenTelemetry, so the SDK project's OpenTelemetry version matrix never changes the
hook.

The alternative **in-process** code entry point needs a project reference to the
source (`AgentRuntime/TraceAI.AutoInstrumentation.Runtime.csproj`); `TraceAIAuto.Init()`
is not available from the NuGet package, which carries no compile asset. Choose one
entry point so lifecycle/diagnostic APIs run in the context that owns the agent:

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
TOML subset is double-quoted strings, booleans, numbers and single-line arrays of
strings. Comments outside strings and table-heading lines are ignored; this is not a
full TOML parser. Malformed lines are ignored, with one exception: a `hide_*` line
is always read (see the hide switches below). Keep credentials out of committed files.
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
| `FI_SAMPLE_RATE` | When set, `ParentBased(TraceIdRatio(rate))`, clamped to 0–1; otherwise standard OTel sampler configuration below. It never changes the application's own sampler. |
| `FI_AUTO_DEBUG` | `false`; print masked configuration/integration diagnostics at startup. |
| `OTEL_SDK_DISABLED` | `true` disables the agent entirely, without missing-key warnings. |

An explicit `AutoOptions.SampleRate`, `FI_SAMPLE_RATE`, or file `sample_rate`
keeps parent-based ratio sampling and wins over `OTEL_TRACES_SAMPLER`; existing
option > environment > file precedence is unchanged. Otherwise, an agent-created
provider honors `OTEL_TRACES_SAMPLER` (`always_on`, `always_off`, `traceidratio`,
`parentbased_always_on`, `parentbased_always_off`, `parentbased_traceidratio`) and
`OTEL_TRACES_SAMPLER_ARG` (a finite ratio from 0 to 1; omitted means 1). Unset
configuration defaults to `ParentBased(TraceIdRatio(1.0))`; invalid or unsupported
configuration falls back to that default with one warning, without echoing values.
An application-owned provider keeps its own sampler, including for an added FI
exporter. The agent never changes the application's sampled flag; only accepted
detached exports are marked sampled, and parent-based choices honor parent flags.

For agent-created providers, accepted exported copies carry `fi.sampling.ratio`, a
double strictly between 0 and 1 when the head-sampling keep probability is known;
absent means 1.0 for estimated counts and costs. Export copies and the explicit
propagation context below carry `fi=r:<ratio>` in W3C `tracestate` while preserving
other entries. Parent-based children of sampled local or remote parents inherit only
a valid upstream `fi` ratio, regardless of the local root rate (including 0 or 1).
This includes the default and `parentbased_always_on`/`parentbased_always_off`.
Non-parent-based samplers never copy upstream ratios; fractional `traceidratio`
decisions stamp their own rate. The metadata survives privacy-fallback export copies
(other tracestate content may be cleared for privacy), but is never stamped on raw
record-only activities, dropped spans, full-rate roots, or application-owned providers.

### Explicit agent-owned propagation (in-process entry point)

The zero-code hook deliberately leaves `Activity.Context`, automatic HTTP header
injection, and application-owned propagation unchanged. **Export metadata alone
does not propagate a new ratio across processes.** To carry the agent's decision
on a handoff you own, use the in-process project reference described above and
explicitly inject `TraceAIAuto.GetPropagationContext()` instead of `Activity.Current.Context`:

```csharp
using OpenTelemetry.Context.Propagation;
using TraceAI.AutoInstrumentation;

// Run inside an active activity observed by the agent's owned provider.
// Use an agent-owned carrier/transport, not one whose headers the host injects.
var headers = new Dictionary<string, string>();
new TraceContextPropagator().Inject(
    new PropagationContext(TraceAIAuto.GetPropagationContext(), default),
    headers, static (carrier, key, value) => carrier[key] = value);
// Send these traceparent/tracestate headers. The receiver extracts them with its
// standard W3C propagator and uses that remote context when starting its activity.
```

The returned value carries the owned agent's sampled decision and valid ratio,
merged into the activity's current vendor tracestate, without mutating its tags,
tracestate, sampled bit, or ambient context. You can pass an explicit `Activity`
instead of using the current one. With a supplied provider, an unobserved activity,
a rejected decision, or an inactive agent, the API returns the ordinary activity
context unchanged (or the default context when no activity exists). It does not
replace a process-global propagator or override existing headers. The hook-only
NuGet package has no compile asset for this API; zero-code applications need this
explicit in-process integration for agent-owned cross-process ratio propagation.

All `FI_HIDE_*` switches from `FIInstrumentation.TraceConfig` are honored in this
path, including flattened message attributes and audio transcript text. Their
lower-case file equivalents work too (`hide_inputs = true`, etc.). A hide switch
reads `true`, `1`, `yes` or `on` as hide and `false`, `0`, `no` or `off` as show;
values are trimmed and case-insensitive, and a blank value counts as not set. Any
other value hides that content and prints one warning that names the variable, so a
mistyped switch never leaves content visible. In the file, a hide value may also be
a single-quoted string or a bare word (`hide_inputs = 'on'`, `hide_inputs = on`),
and a value the file reader cannot decode is kept as written, so it hides with that
warning instead of being skipped. `FI_WIRE_CAPTURE` and `FI_AUTO_DEBUG`
are not hide switches: they read `true`, `1` or `yes` as on and anything else as
off. As in
`TraceConfig`, error details are hidden whenever any input or output content flag
is set, because provider error text can repeat the prompt; an explicit
`FI_HIDE_ERROR_DETAILS` that hides or shows overrides that. Hidden error details keep
the status code and `error.type` but drop the status description, `error.message`,
`exception.message`, `exception.stacktrace` and `otel.status_description` on every
span, including the provider's error text on HTTP client spans. Agent-created
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
`AutoOptions.TraceConfig` without changing existing SDK defaults. A supplied
`TraceConfig` is used as given, and the agent then reads no hide switch itself. A
`TraceConfig` built with `TraceConfig.Builder()` reads `FI_HIDE_*` from the
environment with the SDK's own parsing, not the rule above: only `true`, `1` or
`yes` hide, `on` and unrecognised values do not, and no warning is printed.

Credentials are used only for collector authentication. Provider authorization
headers are never exported; known provider keys are redacted if echoed in a wire
response/error. Credential-named JSON keys at any depth, signed-URL query values,
and credential echoes (including configured FI keys) are replaced by `<redacted>`.
Credential-shaped values are also replaced in every exported string, whatever the
field is called: bare provider keys (`sk-…`), AWS access key IDs, JWTs, `Bearer` and
`Basic` authorization values and private-key PEM blocks (the catalog's
`credentials.value_shapes`). Inline base64 media is replaced by `<omitted: N base64 chars>` before TraceConfig
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
(names containing KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL or AUTH). Such an
environment value longer than 4,096 characters is skipped with one warning that names
the variable, never its value.
`TraceAIAuto.RegisterSecret(value)` lets an integration register additional values;
shorter values are ignored. If the registry itself must fail closed (for example an
overlong registered value), the agent says so once on standard error and exports
spans with identity and timing but no content for the rest of the process. The export boundary takes one immutable, longest-first
snapshot per batch and replaces these values in every exported string, including
unrelated and later spans. There is no per-request secret set, span-start inheritance
or ancestor credential propagation. Registry values never appear in diagnostics.

The HttpClient observer always collects credentials, before sampling, model-span
de-duplication, suppression or `FI_WIRE_CAPTURE=false` can skip capture. For a request
that is not an AI call it reads headers, the query and buffered bodies of at most
16 KiB, so a large request body is never parsed before an ordinary request is sent;
AI requests keep full body discovery.
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
(Activity itself has no bounded tag container on DiagnosticSource 8–11). Reserve
identity, outcome, provider/model/operation, all known usage, timing, provenance and
degradation fields, including one dropped-content marker when necessary. Timing is
the exact keys `gen_ai.response.latency`, `gen_ai.response.time_to_first_chunk`,
`gen_ai.response.time_to_first_text`, `gen_ai.response.time_to_first_token`,
`gen_ai.server.time_to_first_token`, `gen_ai.server.time_per_output_token` and
`gen_ai.server.request.duration`, when present. Atomic
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
usage, timing and provenance families retain their lexical prefix. A zero limit produces a
bounded one-time diagnostic rather than increasing the host's limit. Host Activities,
resources and other exporters' views are unchanged: the agent never writes tags to an
application's Activity. What it needs at start (whether the activity is a model call,
and the FI context of the moment) is kept in custom properties, which exporters do not
read, and the mapping and FI context are applied to the detached export copy.

Value traversal has a shared 4,096-node/4-MiB text budget and a 64-level depth bound;
cycles, expanding graphs and oversized values redact the affected attribute.
The export snapshot additionally bounds the combined attribute/event/link/scope
work to 4,096 nodes/8 MiB per span, 65,536 nodes/8 MiB per batch and 4,096 nodes/4 MiB
per resource. Credential observation is capped at 4 MiB (also respecting the configured
body limit), eight encoding layers, 128 distinct values per discovery and 4,096
characters per value. Registry capacity is 1,024; discovery/value-budget failures
fail closed, while capacity overflow uses LRU eviction. A snapshot failure discards
all content but retains identity, flags and timing. Snapshot shells use the tested
DiagnosticSource 8–11 private content fields to preserve IDs without invoking
ActivitySource constructors or application listeners. These and the other private
runtime and OpenTelemetry members the agent uses are resolved once; at start the
agent prints one warning naming any that are missing. An unrecognized layout omits
telemetry rather than reverting to the unsafe shared Activity. These privacy rules
apply to the agent's exporter, not an application's separately configured exporters.
The agent does not enable libraries' sensitive-content diagnostics automatically; the only exception is the explicit `FI_AUTO_CAPTURE_CONTENT=true` opt-in for ASP.NET Core hosts (see Web hosts below). Any traceAI hide setting keeps that capture off even when the opt-in is set.

## Library layer and transactions

| Integration name | Sources / behavior |
| --- | --- |
| `openai` | `OpenAI.*`; enables `OpenAI.Experimental.EnableOpenTelemetry`. |
| `extensions_ai` | `Experimental.Microsoft.Extensions.AI*`, `Microsoft.Extensions.AI*`. ASP.NET Core hosts: registered `IChatClient` and `IEmbeddingGenerator<,>` services are wrapped automatically (see [Web hosts](#web-hosts-chat-clients-and-agents-without-code)). Other apps: use `OpenTelemetryChatClient`/`UseOpenTelemetry()`. |
| `semantic_kernel` | `Microsoft.SemanticKernel*`; enables `Microsoft.SemanticKernel.Experimental.GenAI.EnableOTelDiagnostics`, not the sensitive-content switch. |
| `azure_inference` | `Azure.AI.Inference*`; enables `Azure.Experimental.EnableActivitySource`. |
| `azure_openai` | `Azure.AI.OpenAI*`; modern clients also emit `OpenAI.*` (enable `openai` too). |
| `agents` | `Experimental.Microsoft.Agents.AI*`, `Microsoft.Agents.AI*`. ASP.NET Core hosts: registered `AIAgent` services are wrapped automatically. Other apps: use the framework's OpenTelemetry wrapper. |
| `aspnetcore` | The exact `Microsoft.AspNetCore` source (not other framework sources under it); incoming SERVER activities, preserving remote parents. A request span is sent only when an AI span of the same trace was captured, so health checks, static files and other plain routes are not sent. |
| `wire` | `HttpHandlerDiagnosticListener` request, response and exception events. |

Listeners cover sources created after startup. `gen_ai.system` and legacy token
usage attributes are mapped to current conventions, and operation names map to
`gen_ai.span.kind` (`LLM`, `EMBEDDING`, `AGENT`, `TOOL`, etc.), on the export copy
only. No blanket outgoing HTTP instrumentation is installed: the wire layer listens
to the HTTP client's request, response and exception events, which create no
Activity, so a request made without an ambient Activity gets no `traceparent` or
`tracestate` header because of the agent. Inside an existing Activity the runtime
sends those headers as it does without the agent. Calls made through AI libraries
whose telemetry the agent enables run inside that library's span, so they carry that
span's trace headers to the provider. Framework AGENT/CHAIN/TOOL spans allow child
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

## Web hosts: chat clients and agents without code

`Microsoft.Extensions.AI` and `Microsoft.Agents.AI` emit spans only through their own OpenTelemetry wrappers
(`OpenTelemetryChatClient`, `OpenTelemetryEmbeddingGenerator<,>`, `OpenTelemetryAgent`), which an application adds with
`UseOpenTelemetry()`. In an ASP.NET Core host the startup hook adds the companion assembly
`TraceAI.AutoInstrumentation.HostingStartup` to `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES` (appended to any existing value, never
replacing it). Just before the host builds its container, that assembly wraps every registered `IChatClient`,
`IEmbeddingGenerator<,>` and `AIAgent` in the library's own wrapper, so the application needs no tracing code.

- A registration whose instance already carries the wrapper (the application called `UseOpenTelemetry()`) is left as it is, so each call produces one span.
- It is off when the agent is not running (`OTEL_SDK_DISABLED`, missing credentials) and per kind when `extensions_ai` or `agents` is disabled (`FI_DISABLED_INSTRUMENTATIONS`, or left out of `FI_INSTRUMENTATIONS`).
- Prompt and response content is off by default. The libraries' sensitive-content switch (`EnableSensitiveData`) puts content on the span for every listener the application has, not only this agent, so the startup leaves it off unless you opt in with `FI_AUTO_CAPTURE_CONTENT=true`. The startup never switches it on against the standard `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` when it is set to anything but `true` (`false`, `False`, `0`). Any traceAI hide setting (`FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`) keeps it off even then. With the opt-in, an application's other listeners or exporters on `Experimental.Microsoft.Extensions.AI` and `Experimental.Microsoft.Agents.AI` receive the content too. The agent's own privacy rules still apply to what it exports.
- The integration status reads `instrumented` with `auto (web host)` once a registered service was wrapped by this startup, and `manual` otherwise, including when the application's own `UseOpenTelemetry()` wrapper is the one tracing.
- Registrations of open generic types and keyed services are left as registered. An instance the application registers stays the application's to dispose. A type registration (`AddSingleton<IChatClient, MyChat>()`, `AddScoped<AIAgent, MyAgent>()`) becomes a factory with the same lifetime: it builds `MyChat` with `ActivatorUtilities` constructor rules (mark the intended constructor with `[ActivatorUtilitiesConstructor]`), wraps it, and returns a small object the container owns. When the container (or scope) is disposed that object releases the wrapper and `MyChat` exactly once: `DisposeAsync` when `MyChat` is `IAsyncDisposable` and the container is disposed asynchronously, otherwise `Dispose`. This is done by the hook itself because the library's agent wrapper (`OpenTelemetryAgent`) does not dispose the agent it wraps. No descriptor is added and no keyed service is used, so it works on any DI container; your own registrations of `MyChat` are neither used nor changed and `MyChat` does not become resolvable. The container no longer validates that constructor at build time; an unsatisfiable constructor fails when the service is first resolved. If the hook cannot build its ownership object for a registration, that registration is built as registered and is not traced (`FI_AUTO_DEBUG=true` prints a `left a registration untraced` line).
- The startup adds the assembly to `ASPNETCORE_HOSTINGSTARTUPASSEMBLIES` in the process environment, so child processes the application starts inherit it. An ASP.NET Core child process that runs without the hook cannot find the assembly and logs a hosting-startup load error; start such children with the variable removed.
- Not covered: applications that are not ASP.NET Core hosts, clients built directly (`new ChatClientBuilder(...).Build()`) and keyed registrations. They call `UseOpenTelemetry()` as before.
- The agent loads next to `System.Diagnostics.DiagnosticSource` 8 through 11 (`Microsoft.Extensions.AI` 11 ships version 11).

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

At the HTTP client's response event, the original response and `HttpContent` are retained. For the
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
`TraceAI.AutoInstrumentation.Runtime.dll`, the FI SDK, pinned OpenTelemetry **1.15.3** and supporting
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
certified. A `DOTNET_STARTUP_HOOKS` path that does not exist fails in the runtime
before the agent runs and cannot be caught by it; the published payload itself loads
in both .NET 8 and .NET 10 processes, and if its runtime files are missing the agent
prints one line and the application continues without tracing.

.NET uses additive ActivitySource listeners rather than one replaceable global
provider. By default the agent builds a side-by-side provider/exporter. With
`AutoOptions.Provider`, it reuses the caller's provider and adds only explicitly
supplied destinations; neither mode disposes or replaces a host provider. Existing
host exporters keep working; overlapping host source subscriptions may
intentionally export the same activity to their own destinations. An activity takes
the most permissive vote of all listeners, so the agent's own provider only ever
votes record-only, never for the sampled flag: the application's sampler alone
decides the sampled flag, what the application exports and what it propagates
downstream, and the application's exporters see the same sampled flags as without
the agent. Record-only is still a vote to record: on sources the application also
follows, activities its sampler did not sample are created with `IsAllDataRequested`
set, so the application's processors and instrumentation callbacks (such as `Filter`
and `Enrich`) also run for them; its export processors skip them because they are
not sampled. The
agent's batch processor enforces the selected head sampler's decision, including
when another listener records a span the agent dropped. With a supplied provider,
only the application's sampled flag gates the agent's added destination.

## Tests

```sh
export DOTNET_HOST_PATH="$(dirname "$(readlink -f "$(command -v dotnet)")")/dotnet"
dotnet build csharp/FIInstrumentation.sln -c Release -m:1
dotnet test csharp/FIInstrumentation.sln -c Release --no-build
```

Build on one node (`-m:1`): the startup hook tests also build their .NET 8 child
applications. The child-process tests need the .NET 8 runtime (including ASP.NET
Core) installed next to the .NET 10 SDK in the same `dotnet` root; `DOTNET_HOST_PATH`
(not the startup hook variable) names that root's `dotnet` executable (without it,
the tests look for `csharp/.dotnet/dotnet`). Tests use no live provider or Future AGI
credentials. The net10 test runner builds the payload and launches actual .NET 8 and
.NET 10 child processes. Stock console probes have **no package or telemetry
references**; richer probes exercise ASP.NET Core, OpenAI and an older host OTel
provider. All send HTTP to a local mock provider; a local collector decodes the
actual OTLP protobuf. The suite reads every current shared fixture (including Gemini
JSON-array streams, multipart messages and Cohere v2 SSE); fixture files are never
copied or changed by tests. Synchronous processor chains and exporter entry/release
events make privacy and blocked-flush regressions deterministic, without sleeps.

- `StartupHookTests`: JSON, SSE byte preservation, async/default-buffered/synchronous
  reads, de-duplication, missing credentials, disabled SDK, sampling, masking, binary
  counting, truncation, idle startup with no network, dead collectors, SERVER-to-wire
  parentage in a real ASP.NET Core host, and native OpenAI .NET client telemetry.
- `HostIsolationTests`: the application with and without the hook. Its own sampling
  at AlwaysOff and at 1% while the hook samples at 1.0, what its exporter sees on a
  shared source, its outgoing request headers, which web request spans are sent, and
  the cost of per-activity lookups and of large non-AI request bodies.
- `ErrorDetailPrivacyTests`: hidden content also hides error details, including a
  provider error that repeats the prompt, in real .NET 8 and .NET 10 applications.
- `AgentStartupTests`: every private runtime member the agent uses resolves on .NET 8
  and .NET 10, a .NET 8 application keeps running with the published payload or with
  its runtime files missing, and credential registry warnings.
- `ValueGraphAndStreamingFlagTests`: environment resource privacy through stock
  startup-hook processes, cyclic and shared value graphs, user-defined `ToString`
  overrides that must never run, and streaming endpoint flags across five framings.
- `GetterSafetyAndHideFlagTests`: getter safety at stop and export, credential header
  whitespace, native content masking in actual OTLP, image URLs, nested Bedrock
  vectors, diagnostic secret masking, destination-write aborts and request-time
  suppression across deferred completion.
- `ExportSnapshotPrivacyTests`: concurrent enrichment after snapshotting, every privacy
  surface through native and compatibility transports, non-finite values, file-backed
  images, ambient getter safety and fail-closed snapshot budgets.
- `TransportParityTests`: fresh no-hook and hooked processes for buffered, synchronous,
  streaming, zero-byte, canceled, partial and unread bodies and helper identity, plus
  scope, tracestate, event and link privacy, de-duplication and transport timestamps.
- `QueryHeaderCredentialAndSigtermTests`: query credential echoes, every recognized
  auth header on all export surfaces, and SIGTERM against no-hook baselines (default
  exit 143 and repeated application-canceled signals followed by new spans).
- `TextHideAndExactLengthTests`: `.text` hide families, percent-escaped data URLs,
  session-token and body-credential echoes under covered native spans, and exact
  Content-Length completion before any EOF read or disposal.
- `KnownSecretRegistryTests`: message events, credential URL vocabularies, the bounded
  LRU registry, registration across load contexts, environment seeding and compressed
  request bodies.
- `MultipartUploadParityTests` and `MediaPathTests`: multipart upload parity and
  credentials, structural audio, data URIs and MIME-tagged media paths on every actual
  OTLP surface, with no-hook baselines.
- `CredentialEchoAndFlushTests`, `LateProcessorAndExporterOwnershipTests`,
  `ActivationAndProjectionTests` and `FrameworkLayoutTests`: header echoes and blocked
  flushes, late application processors and borrowed exporters, neutral activation and
  attribute projection limits, and the HTTP client and Activity layouts the agent uses.

Compatibility protobuf is checked against the pinned upstream exporter for resource
and attribute values, timestamps, events, links and error status, plus its
message-size bound. The older-host coexistence probe also consumes an accepted
response under the host's separate suppression scope on both runtimes. The separate
legacy `csharp/e2e_tests` console program calls a live provider and is not part of
this offline verification.

Unit lifecycle tests explicitly simulate fresh processes using test-only reflection;
production has no reset or reconfiguration API.

## Shared privacy catalog (S6)

`Wire/privacy.json` and `Wire/privacy-fixtures.json` ship byte-identical to
`spec/auto-instrumentation/` (catalog version 3); `sync.py` checks both. The runtime
embeds the catalog, so startup-hook deployment does not depend on a source checkout
or an external privacy file. `PrivacyCatalog.CredentialName` is the one normalized,
path-aware credential predicate, shared by transport headers/query/body harvesting
and every export surface. Query names additionally use the catalog's signed-URL
parameters. Credential ancestry persists through containers and indices; metric and
token-timing exclusions (`time_to_first_token`, `tokens_per_second`, ...) apply only
to their component, not independent credentials. Credential value shapes apply to
every exported string after registered secrets.

Catalog version 3 has no contextual logprob exemption: at generic sites a logprob
`token` is a credential name like any other. Wire spans export parsed messages, not
provider response bodies, so the catalog's `logprob_tokens` provider-response paths
are not used by this agent.

The FI context session id, the exact attribute key `session.id`, is exempt from the
credential name rule on every export surface, as in the other SDKs, so session
grouping works. Its value still has registered secrets and credential-shaped values
removed. Other session-named attributes, such as session tokens, keep the name rule.

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

`ActivationAndProjectionTests` exercises ignored/overridden FI credentials after LRU
churn, public no-credential initialization, independent application resource
identity, actual native/compatibility OTLP limits (128, 8 and 0), dropped counts,
snapshot isolation and gated real-HttpClient streaming milestones. `WireFixtureTests`
recurses through every shared fixture directory, including `fixtures/round6/`, and
replays exact chunk sizes, arrival offsets and mid-frame failures.

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
- **Web request spans:** a request span is sent only when an AI span of its trace
  ended before the request did. An AI call that the request starts but does not wait
  for is sent without its request span. The set of traces with AI spans is bounded
  (4,096 recent traces). Sampling decisions live on their Activities, not in that cache.
- **2026-10-31 — Attribute limits:** no implementation can retain protected fields
  beyond a host limit smaller than that protected set. Such limits deliberately
  drop content and retain the A7 family/lexical prefix; zero emits no attributes
  and warns once. OTel's SDK eviction count and the projection's accumulated
  content-key loss count describe different stages and are not conflated.

- **2026-10-31 — Concatenated gzip members:** credential
  discovery's strict gzip trailer validation compares the combined decoded output
  with the last member's trailer. Valid concatenated-member requests can therefore
  conservatively suppress that request's captured content, even though the BCL/app
  reads them successfully. Discovery failure no longer poisons the process-wide
  registry or unrelated later spans. Request bytes and application behavior are
  unchanged. Prefer a single gzip member, or use the `FI_HIDE_*` controls when
  captured content is unnecessary. Multi-member strict decoding is not fixed here.
- **2026-10-31 — Streaming milestone bounds / encoded and binary streams:**
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
  names containing unexcluded `sig`/`key`/`auth` components (the exact `session.id`
  key is exempt, see above). These are shared recognition semantics, not per-SDK
  exceptions. Use non-credential identifiers for telemetry that must remain visible. Unknown
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
- **2026-10-30 — TOML eval_tags:** string arrays in the flat TOML
  reader are comma-joined, so `eval_tags = ["first,with-comma", "second"]` loses its
  JSON element boundaries. Use `AutoOptions.EvalTags` or a JSON-encoded
  `FI_EVAL_TAGS` environment string when boundaries matter. This is not fixed here.
- **2026-10-30 — Runtime layout/stream identity:** privacy snapshots and standard
  HTTP stream taps depend on the verified DiagnosticSource 8–11 and
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
- **2026-10-30 — Recoverable response-read failures:**
  an already-canceled read or invalid-argument read finalizes capture as ERROR.
  If the application catches it and subsequently consumes the response successfully,
  the application still receives its original HTTP status/bytes, but telemetry can
  remain ERROR with missing output. Capture does not resume after that first failure.
  This telemetry inaccuracy is not fixed here.
- **2026-10-30 — Multipart body limit:** multipart text
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
- **2026-10-30 — Platform coverage:** continuous integration runs the hook tests,
  including older-host telemetry isolation, on macOS and Linux with .NET 8 and .NET 10
  child processes. Windows and other architectures are not exercised. Compatibility
  OTLP on older DiagnosticSource versions does not synthesize remote-parent span flags.
