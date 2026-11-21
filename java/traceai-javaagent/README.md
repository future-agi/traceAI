# traceAI Java agent

Zero-code tracing for Java 17+ applications. Add one `-javaagent` flag and the agent:

* traces AI provider HTTP calls made through the JDK `java.net.http.HttpClient` and OkHttp (OpenAI-compatible APIs, Anthropic, Gemini/Vertex AI, Cohere, Bedrock JSON APIs, Ollama, Deepgram, ElevenLabs, AssemblyAI, Cartesia and reranking APIs), including streaming. OkHttp is tested with versions 3.14.9, 4.12.0 and 5.0.0 only; see [Limitations](#limitations);
* sends only those spans to Future AGI, with credentials and media scrubbed.

The application's own OpenTelemetry setup is left alone. The agent does not register a global provider, and it never wraps, copies or changes the application's providers, span processors, exporters or spans. Spans that the application creates, including spans from the `traceai-java-*` wrappers, go only where the application sends them. On Java 8 to 16 the agent prints one line and the application starts untraced.

## Quickstart

1. Get the agent jar (JDK 17+). It is on Maven Central as `com.futureagi:traceai-javaagent`, released with the Java SDK at the same version:

   ```sh
   VERSION=1.4.1
   curl -fLO "https://repo1.maven.org/maven2/com/futureagi/traceai-javaagent/$VERSION/traceai-javaagent-$VERSION.jar"
   ```

   Or build it from this repository (Maven and JDK 17+):

   ```sh
   mvn -B -f java/pom.xml -pl traceai-javaagent -am -DskipTests package
   # The jar is java/traceai-javaagent/target/traceai-javaagent-<version>.jar
   VERSION=...  # the <version> in that file name
   ```

2. Run your application with the agent and your Future AGI keys:

   ```sh
   export FI_API_KEY="your-api-key"
   export FI_SECRET_KEY="your-secret-key"
   export FI_PROJECT_NAME="my-project"   # optional
   java -javaagent:/path/to/traceai-javaagent-$VERSION.jar -jar app.jar
   ```

3. Check the setup without sending anything:

   ```sh
   java -jar traceai-javaagent-$VERSION.jar doctor
   ```

   `doctor --check` (run with `-javaagent` as well) sends one diagnostic span to test the collector.

The jar is self-contained: ByteBuddy, OpenTelemetry, Gson and SLF4J are bundled under private package names, so they never clash with your application's versions. OkHttp is not bundled; the agent only observes the copy your application already uses. No `--add-opens` or boot-classpath flags are needed.

### Starting from code instead

Put the jar on the classpath and initialize before creating HTTP clients:

```java
import ai.traceai.auto.TraceAIAuto;

TraceAIAuto.init();
// Or: TraceAIAuto.init(Map.of("project_name", "checkout", "sample_rate", 0.25));
```

`init()` attaches the agent dynamically. Some JVM policies forbid that; the agent then warns once and the application keeps running. On JDK 21 and later the JVM prints its own warning about a dynamically loaded agent unless the application starts with `-XX:+EnableDynamicAgentLoading`, and later JDKs are expected to refuse dynamic loading by default. Prefer `-javaagent` in production; with it, `init()` attaches nothing. Dynamic attachment cannot add an interceptor to an OkHttp client that was already built.

If the agent started with `-javaagent` but without keys, a later `init(Map.of("api_key", ..., "secret_key", ...))` starts it.

## What gets traced

* **Spans:** one CLIENT span per provider call, created when the response completes, with the parent taken from the request's context. HTTP and transport errors are `ERROR` spans. Messages, tool calls, token usage, model, finish reasons and streaming timing (`gen_ai.response.time_to_first_chunk`, `gen_ai.response.time_to_first_text`) follow the shared traceAI conventions. Context from `ContextAttributes` (session, user, metadata, tags) is attached, subject to the privacy rules below.
* **Incoming requests join the caller's trace:** while a servlet request is handled (`HttpServlet#service` and its overrides, `jakarta.servlet` and `javax.servlet`, which includes Spring MVC, Jersey and CXF servlets on Tomcat, Jetty and Undertow; tested with embedded Tomcat 10.1 and `javax.servlet` 4.0), the request's W3C `traceparent` and `tracestate` headers are the parent of the AI calls made on that request's thread. A sampled caller gets the AI spans in its own trace, under its span, with its `fi=r:` sampling ratio inherited; an unsampled caller (flags `00`) gets nothing exported under the default parent-based sampler. No, or an invalid, `traceparent` starts a new trace as before. No SERVER span is created or sent, and your own active span still takes precedence. The previous context is restored when `service` returns or throws, also when the request goes asynchronous; work the application hands to other threads, and calls made in a servlet filter before the servlet runs, do not see the incoming context. `FI_DISABLED_INSTRUMENTATIONS=servlet` turns this off.
* **Tool executions:** one INTERNAL span named `execute_tool <tool name>` for each tool a LangChain4j or Spring AI application runs, with `gen_ai.operation.name=execute_tool`, `fi.span.kind=TOOL`, `gen_ai.tool.name`, the tool arguments as `input.value` and the result as `output.value`, and the active span as its parent. LangChain4j adds `gen_ai.tool.call.id` (classes that implement `dev.langchain4j.service.tool.ToolExecutor`, which includes the `DefaultToolExecutor` AiServices uses for `@Tool` methods); Spring AI adds `gen_ai.tool.description` (both `call` overloads of classes that implement `org.springframework.ai.tool.ToolCallback`). A failure is `ERROR` with the exception recorded, and the application sees the original exception. The same hide switches apply as for provider calls, and `FI_HIDE_ERROR_DETAILS` keeps exception text out. These spans work on the library classes, not the HTTP client, so they do not need `FI_WIRE_CAPTURE`. `FI_DISABLED_INSTRUMENTATIONS=langchain4j` or `spring-ai` turns each off, and an `FI_INSTRUMENTATIONS` allowlist must list the name to keep it. No second span is made for the traceai libraries' own `TracedToolExecutor` and `TracedToolCallback` (the agent does not advise any `ai.traceai` class except its own), for a call under a TOOL span for that tool, for a call made while Spring AI's own tool-calling observation for that tool is open (recognised by the application's current Micrometer observation, read reflectively, since the tracing handler names and tags its span only when the observation stops; the `framework <tool name>` or `spring.ai.tool` span name and `spring.ai.kind=tool_call` are also accepted), nor for an application subclass of a traceai wrapper, or for another overload of the same callee with the same arguments called from a traced call (`call(String)` delegating to `call(String, ToolContext)`). A tool that calls itself recursively is traced at every level. `gen_ai.tool.name` is not withheld by the hide switches, as in the library wrappers; the description, arguments and result are.
* **Only the agent's spans:** Future AGI receives the agent's own spans and nothing else. A call made under one of your spans gets that span as its parent, but your span itself is not sent.
* **Formats:** every format in the shared wire catalog (`spec/auto-instrumentation/wire-protocols.json`), recognized by protocol rather than client brand, so OpenAI-compatible gateways work too.
* **Audio:** binary and multipart bodies are counted, never buffered; only file name, type, size and bounded text fields are kept.
* **No duplicates under library spans:** a call made while an LLM, EMBEDDING, RERANKER or GUARDRAIL span is the active span (for example inside a `traceai-java-*` wrapper call) is not traced again. The agent reads only the active span: a call under a plain span, such as an HTTP client span below a wrapper span, is traced. Use `try (var ignored = TraceAIAuto.suppress()) { ... }` to skip calls explicitly.
* **Attribute limits:** when a span has more attributes than the host limit allows, identity, outcome, model, usage and timing attributes are kept first, then output, then the newest input. `traceai.capture.dropped_content_count` records how many content attributes were left out.

The agent traces at the HTTP client, not at the SDK. A call from an SDK such as openai-java, anthropic-java, google-genai, LangChain4j or Spring AI is traced when the SDK sends it through the JDK HttpClient, or through OkHttp in one of the tested versions (see [Limitations](#limitations)). The tests drive those two HTTP clients directly; they do not run the SDKs themselves. The agent does not wrap SDK clients. The existing wrappers keep working and `doctor` reports them as `manual`:

```java
new ai.traceai.openai.TracedOpenAIClient(client, tracer);
new ai.traceai.langchain4j.TracedChatLanguageModel(model, tracer, "openai");
new ai.traceai.spring.TracedChatModel(model, tracer, "openai");
```

With `TraceAI.init(...)` and Future AGI keys beside `-javaagent`, the wrappers' own exporter sends each wrapper span once. The agent does not trace the call inside a wrapper span again when that wrapper span is the current span on the thread that sends the HTTP call. Otherwise the same call can be traced twice, once by the wrapper and once by the agent: for example when an SDK sends the request on its own executor thread, where the wrapper span is not current, or when another span, such as an HTTP client span, is current below the wrapper span. A call that the agent skips this way reaches Future AGI only as the wrapper span, through the wrappers' own exporter.

## Privacy

Every span the agent exports passes one export-time privacy boundary based on the shared catalog (`privacy.json`, catalog version 3, checked byte for byte against `spec/auto-instrumentation`):

* Credential-named headers, query parameters, JSON fields and attributes (`authorization`, `api_key`, `client_secret`, `x-amz-signature`, session ids and similar) become `<redacted>`. Usage and timing names such as `max_tokens` or `time_to_first_token` are metrics, not credentials, unless they also carry a credential marker.
* The session id you set with `ContextAttributes.usingSession` is an identifier, not a credential: it is exported as set under `session.id` and `gen_ai.conversation.id`. Known secrets and credential-shaped values inside it are still redacted. Other session-like names keep their redaction.
* Credential values are redacted wherever they appear, even without a name: provider API keys, AWS access keys, JWTs, `Bearer`/`Basic` values and private-key blocks.
* Credentials sent on an instrumented request, configured FI keys and values of credential-named environment variables are remembered process-wide and scrubbed from every later export, including provider error messages. `TraceAIAuto.registerSecret(value)` adds others (eight characters or longer).
* Inline media is omitted: base64 images and audio, data URIs, payloads next to `audio/`, `image/` or `video/` MIME types, and payloads of parts declared as images or video by type (`image`, `input_image`, `image_url`, `video`, `input_video`) or modality. Declared usage counters such as `gen_ai.usage.input_tokens.audio` stay numbers.
* If sanitizing fails or a bound is exceeded, the span is exported with identity and timing only.

Content capture is on by default. Use the hide controls below to restrict it.

## Configuration

Precedence: code arguments, then environment, then `futureagi.toml`, then the fi profile, then defaults. `FI_API_KEY` and `FI_SECRET_KEY` must come as a complete pair from one source; an incomplete source is ignored with one warning. Named fi profiles (`FI_PROFILE`, default `default`) are read from the nearest `.fi/config.toml`, then `$XDG_CONFIG_HOME/fi/config.toml` or `~/.config/fi/config.toml`. Initialization happens once per process: once the agent has started, later `init(options)` calls return the existing provider. A start without keys does not count, so a later `init` with keys can still start the agent.

| Variable | Default / behavior |
|---|---|
| `FI_API_KEY`, `FI_SECRET_KEY` | Required as a pair; without them the agent stays off. |
| `FI_PROFILE` | `default`; selects `[profiles.<name>]` in fi profile files. |
| `FI_BASE_URL` | `https://api.futureagi.com`; spans are sent to `/tracer/v1/traces` as OTLP/HTTP JSON. |
| `FI_ALLOW_INSECURE_HTTP` | Set to `1` to allow plaintext `http://` to a host other than literal loopback or Future AGI. Future AGI hosts always require `https://`; export requests do not follow redirects, and a plaintext send does not use a proxy. Read from the environment (exactly `1`) or the explicit `allow_insecure_http` option (a boolean `true`, or the text `1` or `true`), not from `futureagi.toml` or a profile file. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, then the working directory name, then `DEFAULT_PROJECT_NAME`. |
| `FI_PROJECT_VERSION_NAME`, `FI_PROJECT_VERSION_ID` | `DEFAULT_PROJECT_VERSION_NAME` and a generated UUID. |
| `FI_PROJECT_TYPE` | `observe`. |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | Resource attributes for the agent's own spans; an application-supplied resource takes precedence. |
| `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` / `OTEL_ATTRIBUTE_COUNT_LIMIT` | Span limit, then general limit, then 128. A smaller limit configured in the application SDK is respected. |
| `FI_INSTRUMENTATIONS` | All. Comma-separated allowlist; include `wire` to allow HTTP tracing. |
| `FI_DISABLED_INSTRUMENTATIONS` | None. Denylist wins. `wire` turns HTTP tracing off; `okhttp` or `java.net.http` turns off one client; `servlet` stops joining incoming W3C trace context; `langchain4j` or `spring-ai` stops the `execute_tool` spans of that library. |
| `FI_WIRE_CAPTURE` | `true`. |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304`; per-direction body buffering bound. |
| `FI_SAMPLE_RATE` | When set, `ParentBased(TraceIdRatio(rate))`; `0` drops roots but retains children of sampled parents. Without a rate or OTel sampler setting, defaults to `ParentBased(TraceIdRatio(1.0))`. |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | Used when `FI_SAMPLE_RATE` / `sample_rate` is unset; see sampling below. |
| `FI_AUTO_DEBUG` | `false`; prints the masked configuration and integration statuses at startup. |
| `FI_CONFIG_FILE` | `./futureagi.toml`. |
| `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` | `false`; omit input or output values and messages. |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES` | `false`; omit messages. |
| `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | `false`; omit text, including transcripts and tool arguments. |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | `false`; omit invocation parameters. |
| `FI_HIDE_INPUT_IMAGES` | `false`; omit image and file inputs while keeping text parts. |
| `FI_HIDE_ERROR_DETAILS` | `false`; omit the provider's error text: the status description, `exception.message`, `exception.stacktrace`, `error.message`, `error.stack`, and the `output.value` and `output.mime_type` of a failed call. Partial output messages of a stream that failed partway stay under the output switches (`FI_HIDE_OUTPUTS`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_OUTPUT_TEXT`). Any of the other hide switches in this table (inputs, outputs, messages, text, input images or invocation parameters) hides the error text too. |
| `OTEL_SDK_DISABLED` | `true` disables the agent. |

An explicit `FI_SAMPLE_RATE` (or `sample_rate` in code/config) wins over `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG`; otherwise the agent accepts `always_on`, `always_off`, `traceidratio`, `parentbased_always_on`, `parentbased_always_off`, and `parentbased_traceidratio`. Ratio samplers use `_ARG` in `[0, 1]` (default `1.0`); invalid names or ratio arguments fall back to `ParentBased(TraceIdRatio(1.0))` with one warning. Parent-based choices keep the W3C parent's sampled flag: an unsampled parent's child is dropped. An application-owned provider always keeps its own sampler; these settings affect only the agent's isolated provider.

On the agent's own provider, sampled spans carry the double attribute `fi.sampling.ratio` for a known partial keep probability (`0 < r < 1`), allowing estimated counts and cost weighted by `1/r`; absence means `1.0`. A partial ratio decision writes W3C tracestate `fi=r:<ratio>` and preserves other entries from valid parents; parent-based children inherit only a valid ratio from a sampled parent's `fi` entry (local or remote), retaining that entry. Only a valid span context counts as a parent: an invalid or absent parent makes a locally decided root and its inherited tracestate is reset (all vendors, including stale `fi`), even on a dropped root; a kept partial root writes only its own `fi`, while a full-rate root stays empty. This includes the default sampler, `parentbased_always_on/off`, and parent-based root rates `0` or `1` (including `FI_SAMPLE_RATE=1`). Roots at rate `1` do not gain an attribute or `fi` entry, and upstream sampled parents without valid metadata never get an invented ratio. Non-parent-based always-on/off samplers never copy ratios; non-parent-based `traceidratio` uses only its own partial ratio. Dropped/record-only spans and application-owned providers are not stamped. Sampling metadata counts against the effective attribute budget, including an inherited zero budget.

Each `FI_HIDE_*` switch in the table above, its `init` option or its
`futureagi.toml` setting (`hide_inputs` and so on) reads `true`, `1`, `yes` or
`on` as hide and `false`, `0`, `no` or `off` as show, ignoring case and
surrounding spaces; a blank value counts as not set. Any other value hides that
content and prints one warning that names the variable and the accepted values,
so a mistyped switch never leaves content visible. In `futureagi.toml` a hide
value may be a boolean, a number, a string in double or single quotes, or a bare
word (`hide_inputs = on`); a hide line whose value cannot be decoded is kept as
written, so it hides with that warning instead of being skipped. A `FI_HIDE_*`
variable that is not in the table is not read: any value of it does nothing and
prints nothing. `FI_WIRE_CAPTURE`, `FI_AUTO_DEBUG` and `OTEL_SDK_DISABLED` are
not hide switches: they are on only for `true`, in any letter case.

```toml
# futureagi.toml (a flat subset: strings, booleans, numbers, one-line string arrays)
project_name = "checkout-agent"
instrumentations = ["openai", "langchain4j", "wire"]
sample_rate = 0.25
debug = false
```

A UTF-8 byte order mark at the start of the file is read and ignored, so the
first line counts like any other. A config file that exists but cannot be read,
or that is not valid UTF-8, is not used: none of its settings apply (hide
settings included), and one warning names the file, with its path masked as in
`doctor`, and what failed, never its content; a file that does not exist prints
nothing.

## Doctor and lifecycle

```sh
java -jar traceai-javaagent-$VERSION.jar doctor           # masked configuration and integration report, no network
java -jar traceai-javaagent-$VERSION.jar bootstrap        # dependency inventory only; changes nothing
java -javaagent:traceai-javaagent-$VERSION.jar -jar traceai-javaagent-$VERSION.jar doctor --check
```

In code, `TraceAIAuto.doctor()` returns the same report. Integration statuses are `instrumented`, `pending`, `not_installed`, `missing_instrumentor`, `disabled`, `manual` and `failed`.

`TraceAIAuto.flush()` and `shutdown()` wait up to five seconds by default (overloads take a `Duration`). A shutdown hook drains spans on normal exit. The agent's export queue is separate from your SDK's: your SDK's `forceFlush()` does not flush it, so call `TraceAIAuto.flush()` as well when you need both.

## Limitations

* This release traces AI calls made through the JDK HttpClient and OkHttp. Apache HttpClient, Reactor Netty (Spring WebClient), the AWS SDK v2 clients and gRPC clients are not covered yet, nor are WebSockets and native clients.
* OkHttp is tested with three versions: 3.14.9; 4.12.0 (with Okio 3.6.0 and Kotlin 1.8.21); and 5.0.0 (the `okhttp-jvm` artifact, with Okio 3.15.0 and Kotlin 2.2.0). On each, the tests send a chat call, a streamed call and an asynchronous streamed call through an `OkHttpClient` built by the application. Other OkHttp versions are not verified. OkHttp older than 3.14 is not traced; the application runs unchanged (tested with 3.8.1).
* OkHttp is traced when it is loaded by the system class loader, as with `java -cp` or a plain `-jar` application. When OkHttp sits in a nested or child class loader (Spring Boot executable jars with `BOOT-INF/lib`, WAR files in an application server, Quarkus), OkHttp calls may not be traced; the JDK HttpClient is unaffected. This has not been tested yet.
* Incoming W3C trace context is joined for servlet requests only (`HttpServlet#service`, see [What gets traced](#what-gets-traced)); reactive servers (Spring WebFlux, Netty, Vert.x) and gRPC servers are not covered. The agent creates no SERVER spans, and there is no automatic wrapping of SDK clients.
* The agent needs FI keys to start. It does not yet export to an application-owned OpenTelemetry destination without them.
* Export is OTLP/HTTP JSON in bounded batches, without disk spooling. It has been tested against a local mock collector only.
* Binary Bedrock event streams record request information and status only.
* The agent stays off, with one warning, when the OpenTelemetry Java agent runs in the same JVM, whichever `-javaagent` comes first; the application runs unchanged. When the OpenTelemetry Java agent is listed after this one, the warning appears at the latest when the application first sends a request through the JDK HttpClient or first builds an OkHttp client, before anything is traced; an OkHttp client built from then on gets none of this agent's interceptors. Android, native images, custom HttpClient implementations and isolated custom classloaders are not supported.
* Credentials that an application transforms before sending (re-encoded, split, numeric) are not matched by value; register them with `TraceAIAuto.registerSecret` or use the hide controls.

## Tests

```sh
mvn -B -ntp -f java/pom.xml -pl traceai-javaagent -am verify
```

This runs all shared wire fixtures (`spec/auto-instrumentation/fixtures`), all 6,238 shared privacy fixtures with idempotency checks, configuration and buffering safety tests, and child-JVM tests that run the packaged agent against local mock provider and OTLP servers, on OkHttp 3.14.9, 4.12.0 and 5.0.0, beside the existing wrappers' own exporter and beside the OpenTelemetry Java agent. No real provider or Future AGI request is made. The Java SDK's full-reactor runs skip the child-JVM tests with `-Dtraceai.javaagent.skipITs=true`; they run in the agent's own workflow. A build with `-DskipTests`, `-Dmaven.test.skip=true` or `-Dtraceai.javaagent.skipITs=true`, such as the quickstart's, does not download the OkHttp 4 and 5 jars that those tests use.

The agent checks the target and the ambient `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_EXPORTER_OTLP_TRACES_HEADERS` once at startup. A target or header that may not receive the keys (an `X-Api-Key` / `X-Secret-Key` header name, or a header value that contains a Future AGI key) logs one warning that names no value, installs no exporter and leaves the application unchanged; the same check runs again before each send. Another vendor's keys in those variables are not judged.
