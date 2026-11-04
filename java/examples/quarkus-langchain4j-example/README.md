# Quarkus LangChain4j to Future AGI

Export Quarkus LangChain4j's native AI-service, model and tool spans through Quarkus OpenTelemetry. The indexed `traceai-java-quarkus-langchain4j` dependency supplies error-status contributors and removes `host.name`; the application contains no copied telemetry classes.

Use Java 21 with Quarkus LangChain4j 1.14.2 / Quarkus 3.33.3.1, or the supported floor pair 1.12.0 / 3.33.2. Use the plain `traceai-langchain4j` wrapper only outside Quarkus; adding it here can duplicate model spans.

## Build the example

From the repository's `java/` directory, install the adapter and its parent into your local Maven repository:

```bash
./mvnw -pl traceai-java-quarkus-langchain4j -am install
./mvnw -f examples/quarkus-langchain4j-example/pom.xml package
```

The consumer is a separate Maven project and resolves the installed JAR. The module belongs to the Java reactor and `traceai-bom`.

## Configure and run

Set your own provider credentials and Future AGI keys through your environment or secret store:

```bash
export OPENAI_API_KEY="YOUR_PROVIDER_KEY"
export OPENAI_MODEL="gpt-4o-mini"
export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export FI_PROJECT_NAME="quarkus-weather"
export FI_BASE_URL="https://api.futureagi.com"

java -jar examples/quarkus-langchain4j-example/target/quarkus-app/quarkus-run.jar \
  "What is the weather in Paris?"
```

`application.properties` sets HTTP/protobuf export, both authentication headers and the project resource attributes. Quarkus appends `/v1/traces` to its configured endpoint, so the setting ends at `/tracer`, not `/tracer/v1/traces`.

Quarkus owns the provider, exporter and shutdown. The adapter creates no spans and installs no second provider. Normal JVM shutdown flushes queued spans; forced termination can lose the last batch.

## Privacy and migration

Keep the four `quarkus.langchain4j.tracing.include-*` switches false and `quarkus.otel.span.event.count.limit=0` as shown in `application.properties`. This keeps prompts, completions, tool arguments/results and exception events out of exported traces. Failed AI-service, model and tool spans still receive `ERROR` with an empty status message. `host.name` is removed after Quarkus builds the resource.

To include error messages in span status, set `FI_EXPORT_ERROR_DETAILS=true`. Content switches and the event limit remain separate opt-ins. A tool failure may be returned to the model as a tool result; enabling prompt capture can therefore disclose that error text too. Application logging is separate from trace export.

If you previously copied `FutureAgiTracingDefaults` and `ToolErrorStatus`, remove those application classes when adding the package. Keep your existing exporter and privacy configuration and `FI_EXPORT_ERROR_DETAILS` setting. Do not add a second OpenTelemetry SDK or the plain LangChain4j wrapper.

Native `gen_ai.prompt` and `gen_ai.completion` remain span attributes when enabled; this adapter does not convert them into Future AGI's Input/Output fields. AI-service spans retain their native names without aggregated token counts. The supported route is JVM synchronous model/tool calls; native-image and streaming support are not claimed.

## Offline regression tests

The JUnit tests use a scripted localhost model server and an OTLP/HTTP recorder. `PackagedAdapterTest` verifies CDI discovery from the JAR's Jandex index, while `UpstreamDefaultsTest` demonstrates why the error/privacy contributors are necessary.

```bash
# From java/, after installing the adapter
./mvnw -f examples/quarkus-langchain4j-example/pom.xml clean test

# Repeat at the supported floor
./mvnw -f examples/quarkus-langchain4j-example/pom.xml clean test \
  -Dquarkus-langchain4j.version=1.12.0 -Dquarkus.platform.version=3.33.2

# Exercise production JVM shutdown with the batch exporter
./mvnw -f examples/quarkus-langchain4j-example/pom.xml package \
  -DskipTests -Dquarkus.otel.simple=false
./mvnw -f examples/quarkus-langchain4j-example/pom.xml failsafe:integration-test failsafe:verify
```

CI downloads dependencies first, then runs these tests with networking restricted to loopback. `run_e2e.sh` also supports optional real-service execution when explicitly configured with credentials; it is not needed for the offline suite.
