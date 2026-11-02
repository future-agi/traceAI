# Native Anthropic Java tracing

Use the Anthropic Java SDK's native OpenTelemetry spans with the standard OTLP/HTTP exporter. This example needs Java 17+, Anthropic Java 3.2.0+, and OpenTelemetry 1.57.0+. The POM pins Anthropic 3.3.0.

Set your credentials through your environment or secret manager:

```bash
export ANTHROPIC_API_KEY="<anthropic-api-key>"
export FI_API_KEY="<future-agi-api-key>"
export FI_SECRET_KEY="<future-agi-secret-key>"
export FI_PROJECT_NAME="anthropic-java"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://api.futureagi.com/tracer/v1/traces"
mvn compile exec:java -Dexec.mainClass=AnthropicTracingExample
```

Run from this example directory. The endpoint is the full traces URL. For self-hosting, replace the API host. Open the `anthropic-java` project in Future AGI Observe and find `anthropic.messages.create`.

`AnthropicTracingExample.java` configures a single provider and a batching exporter, then uses the unwrapped SDK client. If your application already has a provider, add the exporter there instead of registering another global provider. The provider closes after the request, flushing pending spans; wait for async calls and close streams before shutting it down.

The example selects metadata-only tracing explicitly. To export prompt and response content, change `OpenTelemetryContentMode.METADATA_ONLY` to `CONTENT`, after reviewing your access and retention settings. Native tracing uses Anthropic's controls, not traceAI's `FI_HIDE_*` settings. Input-token usage includes cache reads and writes; do not add cache tokens again.

Do not combine native tracing with `TracedAnthropicClient`. The released `com.futureagi:traceai-java-anthropic:1.0.0` wrapper remains available for existing blocking-call integrations. If retaining it with a modern SDK, build that SDK client with `.openTelemetry(false)` so only the wrapper emits request spans. No wrapper or traceAI initialization is needed for the native example.

## Contract tests

```bash
mvn test
mvn -Danthropic.version=3.2.0 clean test
```

Tests use the real SDK against loopback API and OTLP receivers, with synthetic credentials. They cover sync/async requests, complete and cancelled streams, tool-runner parenting, privacy, cache-inclusive usage, protobuf with and without gzip, the documented executable in a fresh process, and the duplicate-span boundary with the published legacy wrapper. The Java exporter uses protobuf; the example does not implement a JSON exporter.
