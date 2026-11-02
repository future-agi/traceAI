# Spring AI tracing

Use Spring AI's native Micrometer observations and Spring Boot's OpenTelemetry pipeline. `SpringAIObservationFilter` enriches existing model, embedding, and tool spans for Future AGI; it does not create spans or a second provider/exporter.

> The adapter in this source tree is not in the old Maven Central `1.0.0` artifact, which targeted Spring AI `1.0.0-M4`. Until the next Java SDK release, build this checkout locally with `mvn -f java/pom.xml -pl traceai-spring-ai -am install`. Do not install the M4 artifact into a modern Spring AI application.

## Configure Spring Boot

Keep your existing Spring AI model configuration. Add `com.futureagi:traceai-spring-ai` at the version built from this checkout, `spring-boot-starter-actuator`, `micrometer-tracing-bridge-otel`, and `opentelemetry-exporter-otlp`. Let your Boot BOM manage Micrometer and OpenTelemetry. Boot 4.1 also needs `org.springframework.boot:spring-boot-micrometer-tracing-opentelemetry` for its native tracing auto-configuration.

Register the maintained adapter as a normal Spring bean:

```java
import ai.traceai.spring.SpringAIObservationFilter;
import io.micrometer.observation.ObservationFilter;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
public class SpringAITracingConfiguration {
    @Bean
    ObservationFilter springAIObservationFilter() {
        return new SpringAIObservationFilter();
    }
}
```

For **Spring Boot 3.5**, configure OTLP/HTTP export:

```properties
management.tracing.sampling.probability=1.0
management.otlp.tracing.endpoint=${FI_BASE_URL}/tracer/v1/traces
management.otlp.tracing.headers.X-Api-Key=${FI_API_KEY}
management.otlp.tracing.headers.X-Secret-Key=${FI_SECRET_KEY}
management.otlp.tracing.headers.fi-project-name=${FI_PROJECT_NAME}
management.opentelemetry.resource-attributes.project_name=${FI_PROJECT_NAME}
```

For **Spring Boot 4.1**, use the new export property prefix:

```properties
management.tracing.sampling.probability=1.0
management.opentelemetry.tracing.export.otlp.endpoint=${FI_BASE_URL}/tracer/v1/traces
management.opentelemetry.tracing.export.otlp.headers.X-Api-Key=${FI_API_KEY}
management.opentelemetry.tracing.export.otlp.headers.X-Secret-Key=${FI_SECRET_KEY}
management.opentelemetry.tracing.export.otlp.headers.fi-project-name=${FI_PROJECT_NAME}
management.opentelemetry.resource-attributes.project_name=${FI_PROJECT_NAME}
```

Set `FI_BASE_URL` to `https://api.futureagi.com` (no trailing slash), your Future AGI keys through your secret manager, and `FI_PROJECT_NAME` to your project. Keep your own model-provider credentials separate. Sampling at `1.0` captures all requests; lower it if appropriate for production.

Use your existing `ChatModel`/`ChatClient`, embedding model, and tool configuration unchanged. Models constructed manually must receive the application's `ObservationRegistry`. This path does not use `traceai-spring-boot-starter`; remove that starter when migrating. Do not call `TraceAI.init(...)`, install a second tracing handler, or wrap a natively observed model in `TracedChatModel`/`TracedEmbeddingModel`. A native HTTP client span alongside a model span is expected, not a duplicate model span.

## Content and privacy

The default constructor is metadata-only. To opt in to bounded prompt/embedding/tool input and completion/tool output, use:

```java
return new SpringAIObservationFilter(true, true);
// captureInputs, captureOutputs: enable either independently.
```

The adapter limits each content attribute to 32,000 characters and never copies embedding vectors or binary media. These options govern only this filter's content, not error messages or attributes/logs produced by other instrumentation. Leave separate prompt/completion logging and tool-content filters disabled unless you need them; logs are not trace content.

Model metadata and usage come from Spring AI's native observations. Missing provider usage remains a framework/provider concern; the adapter does not estimate tokens or cost. Tool/model operations retain their native parentage, so use a common application request or parent observation when they should share a trace.

## Existing wrapper callers

`TracedChatModel`, `TracedEmbeddingModel`, and their constructors remain available for models **without native tracing**. The wrappers use modern Spring AI APIs, trace embedding convenience calls, and defer each streaming span until subscription. Each subscription ends once on completion, failure, or cancellation. Stream content is bounded and the latest reported token usage is retained without summing cumulative chunks.

For a wrapper stream subscribed on another thread, bind an explicit parent and TraceAI attributes using `SpringAITracingContext.withContext(...)`. Prefer the native path for new integrations; do not combine both paths on one model.
