package ai.traceai.spring.compatibility;

import ai.traceai.spring.SpringAIObservationFilter;
import com.sun.net.httpserver.HttpServer;
import io.micrometer.observation.ObservationFilter;
import io.micrometer.observation.ObservationRegistry;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import static org.assertj.core.api.Assertions.assertThat;

@Timeout(60)
class DocumentedBootSetupTest {
    @Test void documentedBeanAndPropertiesExportRealModelThroughBootsOnlyProvider() throws Exception {
        List<ExportTraceServiceRequest> received = new CopyOnWriteArrayList<>();
        List<List<String>> headers = new CopyOnWriteArrayList<>();
        var receiver = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        receiver.createContext("/tracer/v1/traces", exchange -> {
            received.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody()));
            headers.add(List.of(exchange.getRequestHeaders().getFirst("X-Api-Key"),
                exchange.getRequestHeaders().getFirst("X-Secret-Key"),
                exchange.getRequestHeaders().getFirst("fi-project-name")));
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        receiver.start();
        String endpoint = "http://127.0.0.1:" + receiver.getAddress().getPort() + "/tracer/v1/traces";
        try (var modelServer = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            modelServer.reply(NativeSpringAITest.CHAT);
            new ApplicationContextRunner().withUserConfiguration(Application.class)
                // Boot 3 and Boot 4 moved the export properties. Only the matching set is used.
                .withPropertyValues("management.tracing.sampling.probability=1.0",
                    "management.otlp.tracing.endpoint=" + endpoint,
                    "management.otlp.tracing.headers.X-Api-Key=synthetic-api-key",
                    "management.otlp.tracing.headers.X-Secret-Key=synthetic-secret-key",
                    "management.otlp.tracing.headers.fi-project-name=spring-ai-test",
                    "management.opentelemetry.tracing.export.otlp.endpoint=" + endpoint,
                    "management.opentelemetry.tracing.export.otlp.headers.X-Api-Key=synthetic-api-key",
                    "management.opentelemetry.tracing.export.otlp.headers.X-Secret-Key=synthetic-secret-key",
                    "management.opentelemetry.tracing.export.otlp.headers.fi-project-name=spring-ai-test",
                    "management.opentelemetry.resource-attributes.project_name=spring-ai-test")
                .run(context -> {
                    assertThat(context).hasNotFailed().hasSingleBean(OpenTelemetry.class)
                        .hasSingleBean(SdkTracerProvider.class).hasBean("springAIObservationFilter");
                    var model = Models.chat(modelServer.url(), context.getBean(ObservationRegistry.class));
                    assertThat(model.call(new Prompt(NativeSpringAITest.INPUT)).getResult().getOutput().getText())
                        .isEqualTo(NativeSpringAITest.OUTPUT);
                    assertThat(context.getBean(SdkTracerProvider.class).forceFlush().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
                });
            assertThat(headers).isNotEmpty().containsOnly(List.of("synthetic-api-key", "synthetic-secret-key", "spring-ai-test"));
            var resources = received.stream().flatMap(r -> r.getResourceSpansList().stream()).toList();
            assertThat(resources).allSatisfy(r -> assertThat(r.getResource().getAttributesList()).anySatisfy(a -> {
                assertThat(a.getKey()).isEqualTo("project_name");
                assertThat(a.getValue().getStringValue()).isEqualTo("spring-ai-test");
            }));
            var models = resources.stream().flatMap(r -> r.getScopeSpansList().stream())
                .flatMap(s -> s.getSpansList().stream()).filter(s -> s.getAttributesList().stream()
                    .anyMatch(a -> a.getKey().equals("fi.span.kind") && a.getValue().getStringValue().equals("LLM"))).toList();
            assertThat(models).hasSize(1);
            assertThat(received.toString()).doesNotContain(NativeSpringAITest.INPUT, NativeSpringAITest.OUTPUT);
        } finally { receiver.stop(0); }
    }

    @Configuration(proxyBeanMethods = false)
    @EnableAutoConfiguration
    static class Application {
        @Bean
        ObservationFilter springAIObservationFilter() {
            return new SpringAIObservationFilter();
        }
    }
}
