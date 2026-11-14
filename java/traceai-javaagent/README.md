# traceAI Java agent

Zero-code tracing for Java 17+ applications. Add one `-javaagent` flag and the agent:

* traces AI provider HTTP calls made through the JDK `java.net.http.HttpClient` and OkHttp 3.14+ (OpenAI-compatible APIs, Anthropic, Gemini/Vertex AI, Cohere, Bedrock JSON APIs, Ollama, Deepgram, ElevenLabs, AssemblyAI, Cartesia and reranking APIs), including streaming;
* sends only those spans to Future AGI, with credentials and media scrubbed.

The application's own OpenTelemetry setup is left alone. The agent does not register a global provider, and it never wraps, copies or changes the application's providers, span processors, exporters or spans. Spans that the application creates, including spans from the `traceai-java-*` wrappers, go only where the application sends them. On Java 8 to 16 the agent prints one line and the application starts untraced.

## Quickstart

1. Build the agent jar from this repository (Maven and JDK 17+). It is not on Maven Central yet: it is held from the Java SDK releases until its first release is approved.

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
* **Only the agent's spans:** Future AGI receives the agent's own spans and nothing else. A call made under one of your spans gets that span as its parent, but your span itself is not sent.
* **Formats:** every format in the shared wire catalog (`spec/auto-instrumentation/wire-protocols.json`), recognized by protocol rather than client brand, so OpenAI-compatible gateways work too.
* **Audio:** binary and multipart bodies are counted, never buffered; only file name, type, size and bounded text fields are kept.
* **No duplicates under library spans:** a call made while an LLM, EMBEDDING, RERANKER or GUARDRAIL span is the active span (for example inside a `traceai-java-*` wrapper call) is not traced again. The agent reads only the active span: a call under a plain span, such as an HTTP client span below a wrapper span, is traced. Use `try (var ignored = TraceAIAuto.suppress()) { ... }` to skip calls explicitly.
* **Attribute limits:** when a span has more attributes than the host limit allows, identity, outcome, model, usage and timing attributes are kept first, then output, then the newest input. `traceai.capture.dropped_content_count` records how many content attributes were left out.

SDKs such as openai-java, anthropic-java, google-genai, LangChain4j and Spring AI are traced when they send requests through the JDK HttpClient or OkHttp. The agent does not wrap SDK clients. The existing wrappers keep working and `doctor` reports them as `manual`:

```java
new ai.traceai.openai.TracedOpenAIClient(client, tracer);
new ai.traceai.langchain4j.TracedChatLanguageModel(model, tracer, "openai");
new ai.traceai.spring.TracedChatModel(model, tracer, "openai");
```

With `TraceAI.init(...)` and Future AGI keys beside `-javaagent`, the wrappers' own exporter sends each wrapper span once, and the agent does not trace the call inside it again.

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
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, then the working directory name, then `DEFAULT_PROJECT_NAME`. |
| `FI_PROJECT_VERSION_NAME`, `FI_PROJECT_VERSION_ID` | `DEFAULT_PROJECT_VERSION_NAME` and a generated UUID. |
| `FI_PROJECT_TYPE` | `observe`. |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | Resource attributes for the agent's own spans; an application-supplied resource takes precedence. |
| `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` / `OTEL_ATTRIBUTE_COUNT_LIMIT` | Span limit, then general limit, then 128. A smaller limit configured in the application SDK is respected. |
| `FI_INSTRUMENTATIONS` | All. Comma-separated allowlist; include `wire` to allow HTTP tracing. |
| `FI_DISABLED_INSTRUMENTATIONS` | None. Denylist wins. `wire` turns HTTP tracing off; `okhttp` or `java.net.http` turns off one client. |
| `FI_WIRE_CAPTURE` | `true`. |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304`; per-direction body buffering bound. |
| `FI_SAMPLE_RATE` | `1.0`; parent-based ratio sampler. |
| `FI_AUTO_DEBUG` | `false`; prints the masked configuration and integration statuses at startup. |
| `FI_CONFIG_FILE` | `./futureagi.toml`. |
| `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` | `false`; omit input or output values and messages. |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES` | `false`; omit messages. |
| `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | `false`; omit text, including transcripts and tool arguments. |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | `false`; omit invocation parameters. |
| `FI_HIDE_INPUT_IMAGES` | `false`; omit image and file inputs while keeping text parts. |
| `OTEL_SDK_DISABLED` | `true` disables the agent. |

```toml
# futureagi.toml (a flat subset: strings, booleans, numbers, one-line string arrays)
project_name = "checkout-agent"
instrumentations = ["openai", "langchain4j", "wire"]
sample_rate = 0.25
debug = false
```

## Doctor and lifecycle

```sh
java -jar traceai-javaagent-$VERSION.jar doctor           # masked configuration and integration report, no network
java -jar traceai-javaagent-$VERSION.jar bootstrap        # dependency inventory only; changes nothing
java -javaagent:traceai-javaagent-$VERSION.jar -jar traceai-javaagent-$VERSION.jar doctor --check
```

In code, `TraceAIAuto.doctor()` returns the same report. Integration statuses are `instrumented`, `pending`, `not_installed`, `missing_instrumentor`, `disabled`, `manual` and `failed`.

`TraceAIAuto.flush()` and `shutdown()` wait up to five seconds by default (overloads take a `Duration`). A shutdown hook drains spans on normal exit. The agent's export queue is separate from your SDK's: your SDK's `forceFlush()` does not flush it, so call `TraceAIAuto.flush()` as well when you need both.

## Limitations

* This release traces AI calls made through the JDK HttpClient and OkHttp 3.x. Apache HttpClient, Reactor Netty (Spring WebClient), the AWS SDK v2 clients and gRPC clients are not covered yet, nor are WebSockets and native clients. OkHttp is tested with 3.14.9; 4.x and 5.x are not yet verified.
* OkHttp is traced when it is loaded by the system class loader, as with `java -cp` or a plain `-jar` application. When OkHttp sits in a nested or child class loader (Spring Boot executable jars with `BOOT-INF/lib`, WAR files in an application server, Quarkus), OkHttp calls may not be traced; the JDK HttpClient is unaffected. This has not been tested yet.
* No server spans for servlet or Spring MVC yet, and no automatic wrapping of SDK clients.
* The agent needs FI keys to start. It does not yet export to an application-owned OpenTelemetry destination without them.
* `FI_HIDE_ERROR_DETAILS` is not yet honored by the agent.
* Export is OTLP/HTTP JSON in bounded batches, without disk spooling. It has been tested against a local mock collector only.
* Binary Bedrock event streams record request information and status only.
* The agent stays off, with one warning, when the OpenTelemetry Java agent runs in the same JVM; the application runs unchanged. Android, native images, custom HttpClient implementations and isolated custom classloaders are not supported.
* Credentials that an application transforms before sending (re-encoded, split, numeric) are not matched by value; register them with `TraceAIAuto.registerSecret` or use the hide controls.

## Tests

```sh
mvn -B -ntp -f java/pom.xml -pl traceai-javaagent -am verify
```

This runs all shared wire fixtures (`spec/auto-instrumentation/fixtures`), all 6,238 shared privacy fixtures with idempotency checks, configuration and buffering safety tests, and child-JVM tests that run the packaged agent against local mock provider and OTLP servers, beside the existing wrappers' own exporter and beside the OpenTelemetry Java agent. No real provider or Future AGI request is made. The Java SDK's full-reactor runs skip the child-JVM tests with `-Dtraceai.javaagent.skipITs=true`; they run in the agent's own workflow.
