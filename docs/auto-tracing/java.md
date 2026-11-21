# Zero-code tracing for Java

The traceAI Java agent traces AI provider calls in a Java application without code changes.
You add one `-javaagent` flag at startup. The agent records each AI provider HTTP call made
through the JDK `java.net.http.HttpClient` or OkHttp, including streaming calls, and sends
those spans to Future AGI. Credentials and inline media are removed before export. Your
application's own OpenTelemetry setup is left as it is: the agent does not register a global
provider or change your providers, processors, exporters or spans.

## Requirements

- Java 17 or later. On Java 8 to 16 the agent prints one line and the application starts
  untraced.
- A Future AGI API key and secret key. Without both keys the agent stays off.
- No `--add-opens` or boot-classpath flags are needed.

## Install

The agent is published on Maven Central as `com.futureagi:traceai-javaagent`, version
`1.4.1`. Download the jar:

```sh
VERSION=1.4.1
curl -fLO "https://repo1.maven.org/maven2/com/futureagi/traceai-javaagent/$VERSION/traceai-javaagent-$VERSION.jar"
```

The jar is self-contained. ByteBuddy, OpenTelemetry, Gson and SLF4J are bundled under private
package names, so they do not clash with your application's versions. OkHttp is not bundled;
the agent observes the copy your application already uses.

## Turn it on

Start your application with the `-javaagent` flag:

```sh
java -javaagent:/path/to/traceai-javaagent-$VERSION.jar -jar app.jar
```

This is the recommended way to run the agent in production.

### Starting from code instead

You can also put the jar on the classpath and initialize before you create HTTP clients:

```java
import ai.traceai.auto.TraceAIAuto;

TraceAIAuto.init();
// Or: TraceAIAuto.init(Map.of("project_name", "checkout", "sample_rate", 0.25));
```

`init()` attaches the agent dynamically. Some JVM policies forbid that; the agent then warns
once and the application keeps running. On JDK 21 and later the JVM prints its own warning
about a dynamically loaded agent unless the application starts with
`-XX:+EnableDynamicAgentLoading`. Dynamic attachment cannot add an interceptor to an OkHttp
client that was already built. When the application already runs with `-javaagent`,
`init()` attaches nothing.

## Send traces to Future AGI

Set your keys before you start the application:

```sh
export FI_API_KEY="your-api-key"
export FI_SECRET_KEY="your-secret-key"
export FI_PROJECT_NAME="my-project"   # optional
java -javaagent:/path/to/traceai-javaagent-$VERSION.jar -jar app.jar
```

| Variable | Default and behaviour |
|---|---|
| `FI_API_KEY`, `FI_SECRET_KEY` | Required as a pair from one source. Without them the agent stays off. |
| `FI_BASE_URL` | `https://api.futureagi.com`. Spans go to `/tracer/v1/traces` as OTLP/HTTP JSON. |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, then the working directory name, then `DEFAULT_PROJECT_NAME`. |
| `FI_PROJECT_VERSION_NAME`, `FI_PROJECT_VERSION_ID` | `DEFAULT_PROJECT_VERSION_NAME` and a generated UUID. |
| `FI_PROJECT_TYPE` | `observe`. |
| `FI_PROFILE` | `default`. Selects `[profiles.<name>]` in fi profile files. |
| `FI_CONFIG_FILE` | `./futureagi.toml`. |
| `FI_SAMPLE_RATE` | When set, `ParentBased(TraceIdRatio(rate))`. Without it, `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG` apply, then `ParentBased(TraceIdRatio(1.0))`. |
| `OTEL_SDK_DISABLED` | `true` turns the agent off. |

Settings are read in this order: code arguments, then environment variables, then
`futureagi.toml`, then the fi profile, then defaults. Named fi profiles are read from the
nearest `.fi/config.toml`, then `$XDG_CONFIG_HOME/fi/config.toml` or
`~/.config/fi/config.toml`.

A `futureagi.toml` file uses a flat subset of TOML (strings, booleans, numbers, one-line
string arrays):

```toml
project_name = "checkout-agent"
instrumentations = ["wire"]
sample_rate = 0.25
debug = false
```

If the agent started with `-javaagent` but without keys, a later
`TraceAIAuto.init(Map.of("api_key", ..., "secret_key", ...))` starts it.

## Hide prompt and response content

Content capture is on by default. Each switch below defaults to `false`.

| Variable | What it hides |
|---|---|
| `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS` | Input or output values and messages. |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES` | Input or output messages. |
| `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | Text, including transcripts and tool arguments. |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | Invocation parameters. |
| `FI_HIDE_INPUT_IMAGES` | Image and file inputs. Text parts are kept. |
| `FI_HIDE_ERROR_DETAILS` | The provider's error text on a failed call. Any of the other hide switches hides the error text too. |

You can also set each switch as an `init` option or in `futureagi.toml`, using the lower-case
name without the `FI_` prefix (for example `hide_inputs = true`).

Accepted values:

- `true`, `1`, `yes` or `on` hides the content.
- `false`, `0`, `no` or `off` shows it.
- Case and surrounding spaces are ignored. A blank value counts as not set.
- Any other value hides that content and prints one warning that names the variable.

Credential-named headers, query parameters and fields become `<redacted>`, and inline base64
images, audio and video are left out, whatever these switches say.

## What gets traced

The agent traces at the HTTP client, not at the SDK. It recognizes AI traffic by its wire
format, so OpenAI-compatible gateways are covered too.

- **Providers:** OpenAI-compatible APIs, Anthropic, Gemini and Vertex AI, Cohere, Bedrock JSON
  APIs, Ollama, Deepgram, ElevenLabs, AssemblyAI, Cartesia and reranking APIs, including
  streaming. The full list is in
  [`wire-protocols.json`](../../spec/auto-instrumentation/wire-protocols.json).
- **HTTP clients:** the JDK `java.net.http.HttpClient`, and OkHttp 3.14.9, 4.12.0 and 5.0.0.
- **SDKs:** calls from SDKs such as openai-java, anthropic-java, google-genai, LangChain4j or
  Spring AI are traced when the SDK sends them through one of those HTTP clients.
- **Spans:** one CLIENT span per provider call, with messages, tool calls in messages, token
  usage, model, finish reasons and streaming timing. HTTP and transport errors are `ERROR`
  spans.
- **Incoming requests:** for servlet requests (`jakarta.servlet` and `javax.servlet`,
  including Spring MVC, Jersey and CXF on Tomcat, Jetty and Undertow), the request's W3C
  `traceparent` and `tracestate` headers become the parent of the AI calls made on that
  request's thread. No SERVER span is created.
- **Only the agent's spans:** Future AGI receives the agent's own spans. Your own spans are
  not sent.
- **Calls under library spans:** a call made while an LLM, EMBEDDING, RERANKER or GUARDRAIL
  span is the active span, such as inside a `traceai-java-*` wrapper call, is not traced
  again. The agent reads only the active span. A call sent on another thread, or made under a
  plain span below the wrapper span, is traced, so it can appear twice. To skip calls on
  purpose, use `try (var ignored = TraceAIAuto.suppress()) { ... }`.

## Turn integrations off or choose them

| Variable | Default and behaviour |
|---|---|
| `FI_INSTRUMENTATIONS` | All. Comma-separated allowlist. Include `wire` to allow HTTP tracing. |
| `FI_DISABLED_INSTRUMENTATIONS` | None. Comma-separated denylist. The denylist wins over the allowlist. |
| `FI_WIRE_CAPTURE` | `true`. |
| `FI_GOOGLE_ADK_SKIP_WRAPPED_CALLS` | `false`. Set `true` to skip the agent's Google ADK span for a model or tool call made through `TracedLlm` or `TracedTool`, so the wrapper's span is the only one. |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304`. Body buffering bound per direction. |

Names you can use in the denylist:

- `wire` turns all HTTP tracing off.
- `java.net.http` turns off the JDK HttpClient only.
- `okhttp` turns off OkHttp only.
- `servlet` stops joining incoming W3C trace context.

```sh
FI_DISABLED_INSTRUMENTATIONS=okhttp,servlet \
  java -javaagent:/path/to/traceai-javaagent-$VERSION.jar -jar app.jar
```

## Check that it works

Print the masked configuration and integration report. This sends nothing:

```sh
java -jar traceai-javaagent-$VERSION.jar doctor
```

Send one diagnostic span to test the connection to the collector:

```sh
java -javaagent:traceai-javaagent-$VERSION.jar -jar traceai-javaagent-$VERSION.jar doctor --check
```

List dependencies without changing anything:

```sh
java -jar traceai-javaagent-$VERSION.jar bootstrap
```

In code, `TraceAIAuto.doctor()` returns the same report. Each integration shows one of
`instrumented`, `pending`, `not_installed`, `missing_instrumentor`, `disabled`, `manual` or
`failed`. Existing `traceai-java-*` wrappers show as `manual`.

Set `FI_AUTO_DEBUG=true` to print the masked configuration and integration statuses at
startup.

## Troubleshooting

- **Nothing is traced.** Check that `FI_API_KEY` and `FI_SECRET_KEY` are both set in the same
  source. A source that sets only one of them is ignored with one warning. If no source sets
  both, the agent stays off.
- **The agent stays off with a warning about the OpenTelemetry Java agent.** The traceAI agent
  does not run beside the OpenTelemetry Java agent in the same JVM. The application runs
  unchanged.
- **OkHttp calls are missing.** OkHttp older than 3.14 is not traced. In a Spring Boot
  executable jar, a WAR in an application server or Quarkus, OkHttp may sit in a nested class
  loader and its calls may not be traced. The JDK HttpClient is unaffected.
- **Spans are missing at exit.** A shutdown hook drains spans on normal exit. The agent's
  export queue is separate from your SDK's, so call `TraceAIAuto.flush()` as well as your
  SDK's `forceFlush()` when you need both.
- **A config file is ignored.** A `futureagi.toml` that cannot be read or is not valid UTF-8
  is not used. One warning names the file.

## Limits

- Only the JDK HttpClient and OkHttp are traced. Apache HttpClient, Reactor Netty (Spring
  WebClient), the AWS SDK v2 clients, gRPC clients, WebSockets and native clients are not
  covered.
- OkHttp is tested with 3.14.9, 4.12.0 and 5.0.0 only. Other versions are not verified.
- Incoming trace context is joined for servlet requests only. Reactive servers (Spring
  WebFlux, Netty, Vert.x) and gRPC servers are not covered.
- The agent needs FI keys to start. It does not export to an application-owned OpenTelemetry
  destination without them.
- Export is OTLP/HTTP JSON in bounded batches, without disk spooling.
- Binary Bedrock event streams record request information and status only.
- Android, native images, custom HttpClient implementations and isolated custom class loaders
  are not supported.
- Values your application re-encodes or splits before sending are not matched by value.
  Register them with `TraceAIAuto.registerSecret(value)` (eight characters or longer), or use
  the hide switches.

## More detail

See the [traceAI Java agent README](../../java/traceai-javaagent/README.md) for the full
configuration table, sampling rules and privacy behaviour.
