# traceAI Java agent

Zero-code AI HTTP tracing for Java 17+. All jar entrypoints use a Java-8-bytecode bootstrap: on Java 8–16 it prints one diagnostic line and returns without loading the implementation, so an injected `-javaagent` cannot prevent the application from starting merely because its JVM is below the tracing floor. This is a new, source-built module, not a claim that an agent artifact has already been published to Maven Central.

## Build and run

From `java/`, with Maven and JDK 17 or later installed:

```sh
# Existing parent Surefire configuration references this test-agent jar directly.
mvn -N dependency:get -Dartifact=net.bytebuddy:byte-buddy-agent:1.14.10
mvn -pl traceai-javaagent -am verify
export FI_API_KEY="your-api-key"
export FI_SECRET_KEY="your-secret-key"
java -javaagent:traceai-javaagent/target/traceai-javaagent.jar -jar app.jar
```

The result is **one shaded agent jar**. ByteBuddy, OpenTelemetry, Gson, SLF4J, and reused core configuration/context helpers have private namespaces. OkHttp and Okio are **provided by the application**, not bundled. The agent does not change existing traceAI wrapper APIs.

Alternatively, add the built agent jar to the application classpath and initialize before creating HTTP clients:

```java
import ai.traceai.auto.TraceAIAuto;

TraceAIAuto.init();
// Or: TraceAIAuto.init(Map.of("project_name", "checkout", "sample_rate", 0.25));
```

Integrations can register credentials discovered outside an instrumented transport with `TraceAIAuto.registerSecret(value)`. Registration is thread-safe and process-wide; values shorter than eight characters are ignored. The final exporter scrubs registered values, longest first, from every exported string, including unrelated application spans and later response/error echoes. Initialization seeds configured FI keys and values of environment variables whose names contain `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL`, or `AUTH`. Transport hooks observe outgoing credential headers (including `X-Amz-Security-Token`, and `Authorization`/`Proxy-Authorization` full values and scheme payloads) and credential/signed query values even when library ancestry suppresses wire tracing; suppressed calls never buffer bodies. Credential-named fields in bodies the agent actually parses are also registered. No per-request or per-context secret groups are retained, and registry contents are never diagnostic output.

`init()` attempts dynamic attachment. JVM policies may disable attachment; it then warns once and leaves the application running. Prefer `-javaagent` for production. `premain` and `agentmain` use the same idempotent initializer. Dynamic attachment cannot retrofit an interceptor into an already-built OkHttp client.

There are no required `--add-opens`, `--add-exports`, or bootstrap-classpath flags. JDK HTTP advice uses JDK-only signatures and reflective calls into the system-loader agent. On JVMs newer than ByteBuddy's declared class-file support, the **relocated agent-only** ByteBuddy experimental reader is enabled; this is not a promise that all future JDKs work. Tests cover JDK 17 and selected premain cases on this checkout's Homebrew JDK 27.

## Configuration

Precedence is code arguments > environment > `futureagi.toml` > fi profile > defaults. `FI_API_KEY` and `FI_SECRET_KEY` resolve atomically: an incomplete source is ignored as a whole with one warning naming the missing half, never combined with another source. Ignored FI values still enter the privacy registry. Named fi profiles (`FI_PROFILE`, default `default`) are read from the nearest ancestor `.fi/config.toml`, then `$XDG_CONFIG_HOME/fi/config.toml` or `$HOME/.config/fi/config.toml` (`USERPROFILE` on Windows); project and user profile pairs are independent sources. Code argument names use `snake_case` (or full `FI_*` names). Initialization is once per process, including premain, dynamic attachment, missing credentials, and shutdown: later `init(options)` calls return the existing provider (or null when inactive), log one line once, and do not read settings or replace providers/exporters. Disable-after-enable and restarting after shutdown are unsupported. Configure before first initialization; use a suppression scope for individual calls. There is no runtime reconfiguration/retired-provider path. Secrets should be environment variables, not committed files.

| Variable | Default / behavior |
|---|---|
| `FI_API_KEY`, `FI_SECRET_KEY` | A complete pair from one source is required. Partial sources warn and fall back atomically; no complete pair disables capture. |
| `FI_PROFILE` | `default`; selects `[profiles.<name>]` in fi profile files. |
| `FI_BASE_URL` | `https://api.futureagi.com`; exports to `/tracer/v1/traces` using OTLP/HTTP JSON. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, then the working directory name, then `DEFAULT_PROJECT_NAME`. Unicode names remain unchanged in resources; non-ASCII HTTP routing header values use UTF-8 percent encoding. |
| `FI_PROJECT_VERSION_NAME`, `FI_PROJECT_VERSION_ID` | `DEFAULT_PROJECT_VERSION_NAME` and a generated UUID. Resources also include `eval_tags = "[]"`, `metadata = "{}"`, and `semantic_convention = "fi"`. |
| `FI_PROJECT_TYPE` | `observe`; existing public SDK defaults are not modified. |
| `OTEL_SERVICE_NAME` | Owned-provider service-name override; `OTEL_RESOURCE_ATTRIBUTES` supplies other resource attributes. Normal OTel fallback is used, never the FI project. An application's supplied resource takes precedence. |
| `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` / `OTEL_ATTRIBUTE_COUNT_LIMIT` | Span-specific limit, then general limit, then 128. Wire spans also respect a smaller limit captured from the application SDK; the agent never raises the host's limit. |
| `FI_INSTRUMENTATIONS` | All. Comma-separated allowlist; include `wire` to allow automatic wire tracing. |
| `FI_DISABLED_INSTRUMENTATIONS` | None. Denylist wins over allowlist. `wire` disables automatic capture; `okhttp` or `java.net.http` disables that transport. |
| `FI_WIRE_CAPTURE` | `true`. |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304`; per-direction text buffering and decompressed-output bound. Unavailable/oversized request bodies keep a span without request content only for catalog providers; unknown hosts still require body validation. Multipart files are counted, never buffered; text fields have an independent 64 KiB per-field cap and bounded aggregate metadata. |
| `FI_SAMPLE_RATE` | `1.0`; parent-based trace-ID-ratio sampler. |
| `FI_AUTO_DEBUG` | `false`; prints masked config and integration statuses at startup. |
| `FI_CONFIG_FILE` | `./futureagi.toml`, relative to the application's working directory. |
| `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` | `false`; omit values, messages, and relevant transcripts. Input hiding also omits tools/parameters. |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES` | `false`; omit flattened messages and raw values that would duplicate them. |
| `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | `false`; omit text on the corresponding side, including audio transcripts, flattened content and tool arguments. Input text hiding also omits tool definitions/parameters. |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | `false`; omit serialized invocation-parameter attributes. |
| `FI_HIDE_INPUT_IMAGES` | `false`; omit image/file URI attributes and raw input values that could duplicate images; redact image/file blocks and URIs inside structured message JSON (including string arrays) while preserving text parts. |
| `OTEL_SDK_DISABLED` | `true` disables the agent. |

`FI_AUTO_INSTRUMENT` is Python-only and is not used by Java. Provider/framework denylist entries affect the manual integration report, not protocol matching; transport-specific denylists are honored as described above. For example, denying `openai` does not selectively remove OpenAI traffic from an enabled `wire` layer.

```toml
# futureagi.toml
project_name = "checkout-agent"
project_type = "observe"
instrumentations = ["openai", "langchain4j", "wire"]
disabled_instrumentations = []
wire_capture = true
wire_max_body_bytes = 4194304
sample_rate = 0.25
debug = false
```

This is deliberately a flat TOML subset: double-quoted strings, booleans, numbers, single-line string arrays, comments outside quotes. Table headers are ignored, not treated as namespaces. Malformed/unreadable settings fall back without crashing the application.

## Coverage

* **JDK `java.net.http.HttpClient`:** synchronous `send` and both public `sendAsync` overloads (including push-promise handlers); request publishers and response subscribers are pass-through tees. The application receives its original buffers, buffer positions, bytes, subscription demand, and response types.
* **OkHttp 3.14+:** constructor advice installs interceptors only after checking the required Chain/RequestBody API. Incompatible older versions are left untouched. Request bodies are written exactly once through a tee. Response, ResponseBody, and source objects are never replaced: advice observes original source reads and closes, without eager source access or reads. Subclass/helper methods, source identity, unread bodies, native decoding, and original exceptions remain application-owned. Reusing a builder does not add duplicate interceptors. Synchronous/asynchronous calls are tested with OkHttp 3.14.9; 4.x/5.x matrices are not verified.
* **Wire formats:** all formats in the packaged, byte-identical shared catalog: OpenAI-compatible chat/completions/responses/embeddings/images/moderation/audio, reranking, Anthropic, Gemini/Vertex, Cohere, Bedrock JSON protocols, Ollama NDJSON, Deepgram, ElevenLabs, AssemblyAI polling, Cartesia. All 340 cross-language fixtures are discovered recursively in `spec/auto-instrumentation/fixtures` at test time: 330 wire cases and the spec's 10 explicitly synthetic decoded-Bedrock recognizer cases. Timed SSE, NDJSON and streamed JSON-array replay applies byte partitions before failure boundaries. Only complete observed frames contribute cumulative usage or first-output milestones.
* **Audio:** binary bodies are counted, not buffered. Multipart files are streamed through a boundary scanner and only filename/type/size and bounded text fields are retained. TTS JSON/SSE audio strings are counted and removed while collecting bounded non-audio metadata. Raw file/audio bytes are never attributes. Header values are never exported. Request credential headers are used only to redact echoed secrets; content encoding is retained for bounded decoding. Inline media, credential-named JSON fields and signed-URL parameters are scrubbed before masking, including in errors and context attributes.
* **Export-time privacy:** one final, non-mutating boundary covers wire and forwarded spans, including typed arrays, status, event names/attributes, links, resources, scopes, schema URLs, tracestate, and attribute names. Legacy wrapper content is classified by side too: `cohere.preamble` is input, and the entire standalone `message.tool_calls` family is output, even when those attributes bypass the wrapper's own masking. Signed-query scrubbing uses a forward-only scanner (including percent-encoded parameter names); long question-mark prompts do not trigger suffix rescanning. Data-URI, diagnostic-field, base64 and signed-query scrubbing use forward-only scanners, not backtracking regular expressions; credential replacement uses precompiled KMP patterns. Before SDK admission, bounded attribute candidates are sanitized with their complete MIME/media context so projection cannot expose a payload by removing its discriminator; the asynchronous batch exporter still performs the authoritative sanitizer pass, including secrets observed after admission. Invocation parameters are derived from an explicit scalar/nested-configuration allowlist for every provider and forwarded invocation-parameter attribute; images, messages, prompt, input and unknown content fields cannot enter that family. S6 recognition is loaded once from the packaged, byte-identical `privacy.json`; the packaged `privacy-fixtures.json` is checked against the spec too. One credential-name predicate (patterns, exclusions and contextual logprob exemptions) drives observation, body harvesting, attribute keys, diagnostics and URL query scrubbing, including response URLs. One media predicate handles magic-byte base64 under any key, exact-boundary data URIs, MIME siblings, flattened/indexed paths and audio payload ancestry. MIME hints bind to exact indexed parents and combine before values are sanitized; metadata boundaries preserve transcripts, voice, counts and format. Audio and magic-recognized media have no minimum size; only unrecognized base64 in generic catalog fields uses the >256-character threshold. `FI_HIDE_INPUT_IMAGES` also follows flattened image type/MIME siblings without hiding adjacent text blocks. Non-finite numbers use OTLP JSON strings rather than failing the batch. Credential-named attributes and JSON fields use the full credential-header set, including Google, Anthropic and Azure subscription keys, even when no supported transport has observed the request. Request credentials are observed even under library-layer suppression or with wire capture disabled. Unsupported/unknown containers become type names without executing application getters or `toString`; cycles, branching, string work, and credential retention are bounded. Any sanitization failure yields identity/timing-only exports. Upstream scrubbing is best-effort.
* **Spans:** CLIENT spans are created at response completion, with request start timestamps and the parent captured at request time. Explicit end timestamps come from transport byte/error observation, before body copying, agent parsing, or application mapping/decoding work. HTTP errors and supported transport errors are ERROR. Message/tool/usage attributes, masking, and session/user/metadata context follow the shared contract and core SDK conventions.
* **Projection and identity:** before writing to the SDK's attribute container, reserve identity, outcome, provider/model/operation, usage, provenance and degradation fields. Output groups win; input messages retain a contiguous newest suffix (oldest input is dropped first), with atomic message/tool/MIME groups and no aggregate input restoring an omitted history. `traceai.capture.dropped_content_count` counts omitted content keys and accumulates previous counts. Protected overflow has deterministic no-content selection; zero-capacity/protected-overflow counters are visible in doctor. Resources and effective attribute limits are captured at request handoff, including an application SDK configured after premain and non-global parent providers. Wire export uses a detached resource view, preserving service/namespace/instance/cloud/custom identity and schema URLs; FI routing remains separate. No application resource or exporter view is mutated.
* **De-duplication:** LLM/EMBEDDING/RERANKER/GUARDRAIL ancestry suppresses wire capture even through intermediate SDK-native spans, including existing Java wrappers' lower-case kinds and `fi.span.kind`. The application's span processor tracks parentage from onStart and checks kinds at request time (kinds may be set after span creation). CHAIN/AGENT/TOOL spans remain parents. Nested captures during transport entry are skipped. `try (var ignored = TraceAIAuto.suppress()) { ... }` suppresses capture explicitly. Legacy exposed OTel suppression context keys are recognized reflectively when available; arbitrary third-party/javaagent-private suppression mechanisms are not guaranteed.

Wire coverage depends on the HTTP transport, not the client brand. openai-java, anthropic-sdk-java, google-genai, LangChain4j, and Spring AI traffic is covered **when it uses the hooked clients**; their full SDK/version matrices have not been executed here. gRPC, WebSockets, native clients, Apache HTTP, AWS CRT, AWS SDK v2 Netty, and AWS SDK v2's default Apache client are **not** covered. Bedrock protocol fixture support does not imply default AWS SDK transport coverage. Binary Bedrock event streams record request-side information and status only when carried by a hooked transport.

## Dedicated library layer and doctor

Java's existing integrations are wrappers, not auto-enable instrumentors. The agent reports them honestly rather than constructing wrappers or changing user beans. Existing wrapper examples (a configured `FITracer tracer` is required):

```java
new ai.traceai.openai.TracedOpenAIClient(client, tracer);
new ai.traceai.anthropic.TracedAnthropicClient(client, tracer);
new ai.traceai.googlegenai.TracedGenerativeModel(client, tracer, modelName);
new ai.traceai.langchain4j.TracedChatLanguageModel(model, tracer, "openai");
new ai.traceai.spring.TracedChatModel(model, tracer, "openai");
new ai.traceai.bedrock.TracedBedrockRuntimeClient(client, tracer);
```

The registry includes all existing Java provider, vector-store, and framework modules. Status labels, with a reason when needed:

* `instrumented`: transport advice installed/transformed.
* `pending`: enabled and awaiting a class load, or the agent is not initialized.
* `not_installed`: target library not visible on the classpath.
* `missing_instrumentor`: target exists but the traceAI wrapper module does not; wire tracing still works where supported.
* `disabled`: configuration excluded the integration.
* `manual`: a wrapper/listener requires user code; the report identifies its class.
* `failed`: attachment/transformation failed; application calls continue unchanged.

```sh
java -jar traceai-javaagent.jar doctor
java -jar traceai-javaagent.jar bootstrap  # inventory only; does not modify Maven/Gradle dependencies
java -javaagent:traceai-javaagent.jar -jar traceai-javaagent.jar doctor --check
```

In-process: `TraceAIAuto.doctor()` returns the masked JSON report. Collector URLs in doctor/startup-debug output have userinfo, query and fragment removed, then pass through the same known-secret sanitizer as other diagnostic values so credentials in URL paths cannot bypass masking. The configured exporter URL is unchanged. `doctor(true)` explicitly emits a diagnostic span and flushes to test the collector. Ordinary startup/doctor performs **no network request**. Reports distinguish not tested, accepted, rejected, and unreachable. A collector test with sample rate zero may produce no diagnostic span. Automatic dependency installation is not implemented; add the corresponding `traceai-java-*`, `traceai-langchain4j`, or `traceai-spring-ai` dependency in your build.

`TraceAIAuto.flush()` and `shutdown()` share a five-second default deadline per call; overloads accept a `java.time.Duration`. Flush calls run on a bounded daemon worker; owned-runtime cleanup uses an independent daemon queue and is never cancelled just because a caller's wait deadline expired. Shutdown drains already-observed span completions/forwarding work before closing the exporter, without retaining reconfiguration leases or waiting for never-consumed bodies. Reentrant waits fail open. A JVM shutdown hook drains normal exits; no signal handlers are replaced. An application that never consumes/closes a streaming response may never complete its capture. Consumer close/cancellation records `OK` plus `traceai.wire.incomplete`; transport/close failures record `ERROR` and preserve parsed partial output. Copied gzip/deflate bodies are decoded before parsing, with optional Brotli/Zstd decoders when available on the application classpath. Unsupported/corrupt encodings set `traceai.wire.undecoded_encoding`.

## Known limitations

Dated 2026-10-31:

* No JDK 8/11 is installed on the verification host. Bootstrap qualification uses Java 8 `--release` compilation, class-file major-version checks (52 for the entrypoint and its helper classes), unsupported-version gate tests, and packaged-agent JDK 17 application-parity tests; a native JDK 8/11 smoke test remains unqualified.
* First-output timing retains at most 8,192 transport offset/timestamp pairs per response. An output event without a retained arrival association has no first-text attribute; loss sets `traceai.wire.incomplete`. Earlier established milestones remain valid. Compressed event-to-transport association is not reconstructed: first-chunk remains observable, first-text is omitted and the stream marked incomplete. Parsing completion/span-end timestamps are never substituted.
* The 10 Bedrock milestone fixtures are decoded-payload recognition tests only. AWS binary event-stream decoding remains request/status-only, not qualified by their synthetic SSE carrier.
* Resource/limit bridging is qualified against OTel Java 1.35. It reads known SDK state without installing a global no-op provider, modifying the host provider, or invoking custom provider accessors. Incompatible SDK internals or restrictive named-module access fail open; custom isolated SDKs and arbitrary future OTel versions require separate qualification.
* Attribute limits below the protected-set size cannot retain every protected field. The deterministic protected prefix is retained with no content, and doctor reports bounded overflow/zero-capacity counters; no host limit is raised to hide this impossibility.

Dated 2026-10-30 (OTHER-class items):

* S6 name classification intentionally redacts session identifiers (including `session.id`), not just authentication tokens. Excluded token/key/usage metadata remains available; short unrecognized base64 without MIME/audio context is not classified as media. This is catalog behavior, not a configurable per-SDK exception. Content capture stays ON by default; use the documented hide controls for stronger restrictions.
* fi profile parsing supports bounded (256 KiB) named-profile files, simple double-quoted/basic and single-quoted/literal string settings (including `#` inside either quote style), and the credential/base URL/project fields only; advanced TOML constructs are not supported. Comments are stripped only outside strings; backslashes are escapes only inside basic strings. Malformed/unreadable or oversized profiles are ignored. Configure a complete pair through environment/explicit arguments for unsupported profile syntax; partial sources never mix.
* Sanitizer path, structure and query work is bounded by the catalog (32 path components/depth, 16,384 nodes/query pairs, 4 MiB text). Exceeding a bound fails closed to identity/timing-only export. This can intentionally omit unusually deep flattened attributes or very large signed URLs; application values and host-owned exporter snapshots are not changed.
* Application SDK `forceFlush()` currently flushes its original processors only and can report success while forwarded spans remain queued in the agent's batch processor; application flush success is not an acknowledgement from the added exporter (independently reconfirmed 2026-10-30). Call `TraceAIAuto.flush()` after the application flush to synchronously drain the forwarding batch exporter within the agent's own deadline. The normal agent shutdown hook drains its own exporter independently.
* This round's full verification targets JDK 17. Newer JDKs and OkHttp 4.x/5.x have not been re-certified. Unsupported custom attribute containers (including wrappers with an unverifiable backing container) become type names rather than executing application getters.
* Unusual OkHttp sources that bypass `Source.read(Buffer,long)` (for example a custom body backed directly by an in-memory Buffer), or native sources that cannot be transformed, may have reduced response attributes. They are never wrapped or read ahead.
* Covered-ancestry tracking is bounded to 16,384 recent spans / 256 parent hops; exotic traces beyond these bounds are not guaranteed to deduplicate. The process registry retains at most 1,024 values / 4 MiB of credential characters, evicting least-recently observed values (exports do not refresh recency). Previously evicted values are no longer known secrets unless observed/registered again. An individual value above 4 MiB, or a registry failure, makes exports fail closed to identity/timing-only snapshots for the rest of the process. Sanitizer budgets can redact oversized text rather than export it.
* Parsed-body credential observation is bounded to 8,192 nodes and 32 levels. If any suffix cannot be inspected within those bounds (including primitive padding arrays), the process registry fails closed: this span and all subsequent exports retain identity/timing only, rather than silently exporting unknown credentials. This privacy fallback does not change application requests, responses, or the application's own exporter snapshots. It remains active until process exit; it is not repaired by `init()` or a later small request.
* Application-transformed secrets (for example extracting a signature into plain text before any observed request, JSON encoded inside strings, numeric re-typing, base64 or splitting) are outside guaranteed value matching, as are unmarked credentials that never cross an instrumented transport. Use `registerSecret` for known string values and `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, the corresponding text/message controls, `FI_HIDE_INPUT_IMAGES`, or `FI_HIDE_LLM_INVOCATION_PARAMETERS` for stronger capture restrictions.
* Manual instrumentation without the agent's working transport hooks discovers only configured FI/environment credentials and explicitly registered values; arbitrary SDK-state discovery is intentionally not implemented. Use the same hide controls above to restrict content. Content capture remains ON by default.
* The invocation-parameter allowlist intentionally omits unknown/new provider options and arbitrary nested metadata/schema payloads. Raw input capture (unless hidden) remains available through `input.value`; content fields are never recovered into invocation parameters.

* The isolated, shaded OTel provider does **not** replace the application's provider. Advice on the application SDK's processor lookup adds a forwarding exporter alongside existing processors, including SDKs/tracers built before dynamic attachment. Application span IDs, parents, attributes, status and timestamps are preserved; existing exporters and application-provider shutdown ownership are unchanged. Forwarded spans use the agent's bounded batch exporter. This bridge is verified against OTel 1.35; incompatible SDK internals fail open. Application exporters do not receive the agent's isolated wire spans.
* No servlet/Spring MVC SERVER-span advice or automatic framework listener registration yet.
* Custom HttpClient implementations, custom isolated classloaders, already-built OkHttp clients at dynamic attachment, and Android/native-image are not supported.
* Retries/replayed request publishers reset the body tee and produce one logical-call span, not separate attempt spans. A final response before upload and connection/mid-upload failures without a complete body are captured only for catalog providers, with no request content; unknown hosts still require validation.
* Export uses bounded asynchronous batches and short timeouts, not durable disk spooling or infinite retries. OTLP JSON has been tested with a local mock collector, not the production Future AGI endpoint.
* Full OpenTelemetry-javaagent interoperability and suppression across arbitrary shaded third-party agents remain follow-up work.

## Verification

`mvn -pl traceai-javaagent -am verify` runs existing core tests, all wire goldens, all 4,985 privacy fixtures plus idempotency checks in `PrivacyFixturesTest.everySharedPrivacyFixtureAndIdempotency`, configuration/buffering safety tests, A8 inclusive token algebra/cumulative-snapshot checks, and real child-JVM `-javaagent` tests against local HTTP/OTLP mock servers. `PrivacyCatalogTest` additionally checks every catalog magic signature across base64 prefix/padding boundaries (standard/URL-safe, padded/unpadded, LF/CRLF-wrapped), unrecognized-byte controls, and detached export snapshots. `AgentE2ETest.paddedMagicMediaIsPrivateInShadedAgentWithoutChangingApplicationData` covers the 31-byte PNG regression through both JDK HttpClient and OkHttp, including forwarded attributes/JSON/response URLs and wire input/output, against no-agent request/response/write-count baselines. It also packages a tracing-free executable `app.jar` with only JDK dependencies, proving the single-agent-jar launch command, and tests one-line dynamic initialization. No real AI provider or Future AGI request is made by these tests.
