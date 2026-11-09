# Spring AI tracing

Use Spring AI's native Micrometer observations and Spring Boot's OpenTelemetry pipeline. `SpringAIObservationFilter` enriches model, embedding, and tool spans for Future AGI. Pair it with `SpringAITracingObservationHandler` to preserve error privacy and avoid reporting incomplete chat operations as successful, using the same native spans and OpenTelemetry provider/exporter.

> The adapter in this source tree is not in the old Maven Central `1.0.0` artifact, which targeted Spring AI `1.0.0-M4`. Until the next Java SDK release, build this checkout locally with `mvn -f java/pom.xml -pl traceai-spring-ai -am install`. Do not install the M4 artifact into a modern Spring AI application.

## Configure Spring Boot

Keep your existing Spring AI model configuration. Add `com.futureagi:traceai-spring-ai` at the version built from this checkout, `spring-boot-starter-actuator`, `micrometer-tracing-bridge-otel`, and `opentelemetry-exporter-otlp`. Let your Boot BOM manage Micrometer and OpenTelemetry. Boot 4.1 also needs `org.springframework.boot:spring-boot-micrometer-tracing-opentelemetry` for its native tracing auto-configuration.

Register the maintained filter and handler below. The handler replaces Boot's generic `DefaultTracingObservationHandler`; if you already define that handler yourself, replace it rather than registering both. For handled tool failures, also wire the exception decorator at the [tool manager construction point](#tool-execution-failures); do not replace application processor beans.

```java
import ai.traceai.spring.SpringAIObservationFilter;
import ai.traceai.spring.SpringAITracingObservationHandler;
import io.micrometer.tracing.handler.DefaultTracingObservationHandler;
import io.micrometer.tracing.otel.bridge.OtelTracer;
import io.micrometer.observation.ObservationFilter;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
public class SpringAITracingConfiguration {
    @Bean
    DefaultTracingObservationHandler defaultTracingObservationHandler(OtelTracer tracer) {
        return new SpringAITracingObservationHandler(tracer);
    }

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

Keep your existing `ChatModel`/`ChatClient`, embedding model, and tool definitions. To record handled tool failures, update the manager's processor argument as shown below. Models constructed manually must receive the application's `ObservationRegistry`. This path does not use `traceai-spring-boot-starter`; remove that starter when migrating. Do not call `TraceAI.init(...)`, register an additional generic tracing handler, or wrap a natively observed model in `TracedChatModel`/`TracedEmbeddingModel`. A native HTTP client span alongside a model span is expected, not a duplicate model span.

## Tool execution failures

Spring AI can turn a thrown tool exception into a tool-result message and let the model recover. The tool-exception decorator marks only the current native `TOOL` observation as `ERROR`, using the original exception. The recovered model calls remain successful; the decorator creates no additional spans and does not infer failures from tool-result text.

Decorate the processor **only when passing it to the manager**. Leave the original processor bean in place: its concrete type, identity, lifecycle, and existing consumers remain unchanged. There is no bean post-processor or proxy. Delegating to Spring AI's existing processor preserves `spring.ai.tools.throw-exception-on-error`, its exception allowlist, and custom application recovery/rethrow policy.

If you already build a tool manager, change only its processor argument, using the **same registry** as the manager. Keep all your other builder settings:

```java
var tracedProcessor = new SpringAIToolExecutionExceptionProcessor(observationRegistry, existingProcessor);
var toolManager = DefaultToolCallingManager.builder()
    .observationRegistry(observationRegistry)
    .toolExecutionExceptionProcessor(tracedProcessor)
    // Keep your existing tool resolver and other manager settings.
    .build();
```

Here `DefaultToolCallingManager` is `org.springframework.ai.model.tool.DefaultToolCallingManager`; `existingProcessor` is your current `org.springframework.ai.tool.execution.ToolExecutionExceptionProcessor`. Give this manager to your existing model/tool-calling configuration. The observation filter and tracing handler alone cannot see errors that Spring AI handles internally.

If Boot currently creates your manager, the following configuration opts into an explicitly constructed manager. It reuses the application's registry, resolver, processor, and optional observation convention. **It replaces Boot's manager auto-configuration**, using native defaults for other manager settings. If you already have a manager bean, update that bean instead of adding a second one.

**Spring AI 2.x:** before adopting this example, carry over any configured tool-call limits, confirmation policies, and resolution-fallback settings to the builder. Those `spring.ai.tools.*` manager settings are not bound by this example; only the exception processor's policy remains auto-configured. Prefer changing your existing configured builder's processor argument rather than rebuilding the manager with defaults.

```java
import ai.traceai.spring.SpringAIToolExecutionExceptionProcessor;
import io.micrometer.observation.ObservationRegistry;
import org.springframework.ai.model.tool.DefaultToolCallingManager;
import org.springframework.ai.tool.execution.ToolExecutionExceptionProcessor;
import org.springframework.ai.tool.observation.ToolCallingObservationConvention;
import org.springframework.ai.tool.resolution.ToolCallbackResolver;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
public class SpringAIToolTracingConfiguration {
    @Bean
    DefaultToolCallingManager toolCallingManager(ObservationRegistry registry,
            ToolCallbackResolver resolver, ToolExecutionExceptionProcessor existingProcessor,
            ObjectProvider<ToolCallingObservationConvention> convention) {
        var manager = DefaultToolCallingManager.builder()
            .observationRegistry(registry)
            .toolCallbackResolver(resolver)
            .toolExecutionExceptionProcessor(new SpringAIToolExecutionExceptionProcessor(registry, existingProcessor))
            // Retain any other manager settings here, including Spring AI 2.x policies/limits.
            .build();
        convention.ifAvailable(manager::setObservationConvention);
        return manager;
    }
}
```

## Content and privacy

The default constructor is metadata-only. To opt in to bounded prompt/embedding/tool input and completion/tool output, use:

```java
return new SpringAIObservationFilter(true, true);
// captureInputs, captureOutputs: enable either independently.
```

For chat models, the opt-ins add `gen_ai.input.messages` and `gen_ai.output.messages` as JSON arrays alongside the existing text `input.value` and `output.value`. They preserve message roles, assistant tool-call IDs/names/arguments, and tool-result IDs and content. A tool request is captured even when the application has not executed it. Completed streams use Spring AI's aggregated response, including assembled tool-call arguments; incomplete streams do not invent a completed response.

The adapter limits each content attribute to 32,000 characters and never copies arbitrary message metadata, embedding vectors, or binary media. Structured arrays remain valid JSON: oversized string fields are truncated and trailing messages/tool calls may be omitted to stay within the limit. These options govern only this filter's content, not attributes/logs produced by other instrumentation. Leave separate prompt/completion logging and tool-content filters disabled unless you need them; logs are not trace content.

Set `FI_HIDE_ERROR_DETAILS=true` to retain error status and exception type while omitting messages and stack traces from native model, embedding, tool, `ChatClient`, and advisor spans. The handler applies this policy before recording an error; the filter alone cannot protect error details. This setting does not redact application logs or spans created by other instrumentation, such as HTTP-client spans. It also does not rewrite tool-result messages returned to the model: if content capture is enabled, those messages can appear as tool output or subsequent model input. Leave content capture disabled to keep them out of these spans. Without it, native error details remain available. An explicit `new SpringAITracingObservationHandler(tracer, true)` also enables this policy.

A chat model, `ChatClient`, or advisor observation stopped without a completed response or a reported error retains `UNSET` status instead of `OK`. This includes streams cancelled by downstream timeouts or by callers. Spring AI does not report the cancellation cause to the observation, so the handler does not fabricate an exception; reported provider failures still retain `ERROR`.

Model metadata and usage come from Spring AI's native observations. Missing provider usage remains a framework/provider concern; the adapter does not estimate tokens or cost. Tool/model operations retain their native parentage, so use a common application request or parent observation when they should share a trace.

## Existing wrapper callers

`TracedChatModel`, `TracedEmbeddingModel`, and their constructors remain available for models **without native tracing**. The wrappers use modern Spring AI APIs, trace embedding convenience calls, and defer each streaming span until subscription. Each subscription ends once on completion, failure, or cancellation. Stream content is bounded and the latest reported token usage is retained without summing cumulative chunks. Request metadata combines the model's configured defaults with non-null per-request overrides. Request and response model names stay separate; a response snapshot is never substituted for an unknown request model.

For a wrapper stream subscribed on another thread, bind an explicit parent and TraceAI attributes using `SpringAITracingContext.withContext(...)`. Prefer the native path for new integrations; do not combine both paths on one model.
