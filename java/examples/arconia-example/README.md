# Arconia to Future AGI

[Arconia](https://docs.arconia.io) owns OpenTelemetry tracing, export and batching. Configure its trace
exporter with your Future AGI endpoint, both authentication headers and a `project_name` resource
attribute. There is no `traceai-arconia` module and no second tracing SDK to initialize.

The example uses Java 21, Spring Boot 4.1.1 and Arconia 0.31.0. It also supports Arconia 0.30.0.

## Error-detail privacy

The existing `com.futureagi:traceai-java-core` library provides
`ai.traceai.privacy.spring.ErrorDetailRedactionConfiguration`. Import it on your application:

```java
@Import(ErrorDetailRedactionConfiguration.class)
```

The configuration wraps Spring-managed span exporters before Arconia uses them. It removes
`exception.message`, `exception.stacktrace` and span-status descriptions inside the JVM, while
preserving error status and exception types. It creates no provider, exporter or spans. Leave
`FI_EXPORT_ERROR_DETAILS` unset or `false`; setting it to `true` explicitly exports raw error details.
This is separate from FITracer's `FI_HIDE_ERROR_DETAILS` setting.

Do not copy the old `ErrorDetailRedaction.java` helper into new applications. The example retains
that class only as a deprecated import-compatible alias. Remove existing copies when migrating
to the library import. Keep the privacy import active before starting the application.

Exporters created outside Spring are not intercepted. Wrap those with
`ErrorDetailRedactingSpanExporter.wrap(exporter)` before passing them to a span processor.
This policy does not redact arbitrary attributes, event names, log messages or metric data.

## Run from this checkout

Build Java core locally first so the example uses the matching checkout rather than an older
registry release. From this directory:

```bash
export JAVA_HOME=/path/to/jdk-21
mvn -f ../../pom.xml -pl traceai-java-core -am install
mvn test

export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export FI_PROJECT_NAME="my-service"
export FI_BASE_URL="http://localhost:14338"
mvn spring-boot:run
# In another terminal:
curl localhost:8080/hello
```

`application.yaml` disables metrics and logs, uses HTTP/protobuf, and reads authentication values
from environment placeholders. Leave the protocol unset: on Arconia 0.30.0 and 0.31.0, the spelling
`http-protobuf` prevents exporter registration. Stop normally to flush the final batch.

## Offline tests

`mvn test` runs against a loopback OTLP recorder without vendor credentials. It covers routing,
headers, resource attributes, one HTTP root span, flush on shutdown, privacy and the raw-error
opt-in. `NativePrivacyImportTest` uses the library import without component scanning or the legacy
helper. To run the earlier Arconia release, use `mvn -Darconia.version=0.30.0 test`.
