package ai.traceai.spring.compatibility;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.openai.TracedOpenAIClient;
import com.openai.client.OpenAIClient;
import com.openai.client.okhttp.OpenAIOkHttpClient;
import com.openai.models.ChatCompletionCreateParams;
import com.openai.models.ChatCompletionMessageParam;
import com.openai.models.ChatCompletionUserMessageParam;
import com.sun.net.httpserver.HttpServer;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationRegistry;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.zip.GZIPInputStream;

import static org.assertj.core.api.Assertions.assertThat;

/** Real provider SDK and OTLP transports must coexist in the documented consumer graph. */
class OpenAITransportTest {
    @AfterEach
    void reset() {
        TraceAI.shutdown();
    }

    @ParameterizedTest(name = "starter-owned={0}, gzip={1}")
    @CsvSource({"true,false", "false,false", "false,true"})
    void modelCallAndTraceExportShareCompatibleTransport(boolean starterOwned, boolean gzip) throws Exception {
        List<ExportTraceServiceRequest> received = new CopyOnWriteArrayList<>();
        List<String> encodings = new CopyOnWriteArrayList<>();
        List<String> contentTypes = new CopyOnWriteArrayList<>();
        List<String> apiKeys = new CopyOnWriteArrayList<>();
        List<String> secrets = new CopyOnWriteArrayList<>();
        List<String> modelRequests = new CopyOnWriteArrayList<>();
        List<String> modelAuth = new CopyOnWriteArrayList<>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/tracer/v1/traces", exchange -> {
            try (exchange) {
                String encoding = exchange.getRequestHeaders().getFirst("Content-Encoding");
                encodings.add(encoding == null ? "none" : encoding);
                contentTypes.add(exchange.getRequestHeaders().getFirst("Content-Type"));
                apiKeys.add(exchange.getRequestHeaders().getFirst("X-Api-Key"));
                secrets.add(exchange.getRequestHeaders().getFirst("X-Secret-Key"));
                var body = "gzip".equals(encoding)
                    ? new GZIPInputStream(exchange.getRequestBody()) : exchange.getRequestBody();
                received.add(ExportTraceServiceRequest.parseFrom(body));
                exchange.getResponseHeaders().set("Content-Type", "application/x-protobuf");
                exchange.sendResponseHeaders(200, -1);
            }
        });
        server.createContext("/v1/chat/completions", exchange -> {
            try (exchange) {
                modelAuth.add(exchange.getRequestHeaders().getFirst("Authorization"));
                modelRequests.add(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
                byte[] response = """
                    {"id":"chatcmpl-synthetic","object":"chat.completion","created":1,
                     "model":"gpt-4o-mini","choices":[{"index":0,"message":{"role":"assistant",
                     "content":"private-answer"},"finish_reason":"stop"}],
                     "usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}
                    """.getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, response.length);
                exchange.getResponseBody().write(response);
            }
        });
        server.start();
        String baseUrl = "http://127.0.0.1:" + server.getAddress().getPort();
        try {
            var runner = new ApplicationContextRunner().withUserConfiguration(Application.class)
                .withPropertyValues("openai.base-url=" + baseUrl + "/v1", "OPENAI_API_KEY=synthetic-openai-key",
                    "spring.application.name=my-spring-app", "management.tracing.sampling.probability=1.0",
                    "traceai.hide-inputs=true", "traceai.hide-outputs=true");
            if (starterOwned) {
                runner = runner.withPropertyValues("traceai.base-url=" + baseUrl,
                    "traceai.api-key=synthetic-key", "traceai.secret-key=synthetic-secret",
                    "traceai.project-name=my-spring-app", "traceai.export-interval-ms=86400000",
                    // Keep Boot's native spans off the wire in the starter-owned case.
                    "management.otlp.tracing.export.enabled=false",
                    "management.opentelemetry.tracing.export.otlp.enabled=false");
            } else {
                // Boot 3 and 4 use different property prefixes; each ignores the other's.
                runner = runner.withPropertyValues(
                    "management.otlp.tracing.endpoint=" + baseUrl + "/tracer/v1/traces",
                    "management.otlp.tracing.headers.X-Api-Key=synthetic-key",
                    "management.otlp.tracing.headers.X-Secret-Key=synthetic-secret",
                    "management.otlp.tracing.compression=" + (gzip ? "gzip" : "none"),
                    "management.opentelemetry.tracing.export.otlp.endpoint=" + baseUrl + "/tracer/v1/traces",
                    "management.opentelemetry.tracing.export.otlp.headers.X-Api-Key=synthetic-key",
                    "management.opentelemetry.tracing.export.otlp.headers.X-Secret-Key=synthetic-secret",
                    "management.opentelemetry.tracing.export.otlp.compression=" + (gzip ? "gzip" : "none"),
                    "management.opentelemetry.resource-attributes.project_name=my-spring-app");
            }
            runner.run(context -> {
                assertThat(context).hasNotFailed().hasSingleBean(TracedOpenAIClient.class);
                Observation.createNotStarted("native-parent", context.getBean(ObservationRegistry.class))
                    .observe(() -> {
                        var response = context.getBean(TracedOpenAIClient.class).createChatCompletion(
                            ChatCompletionCreateParams.builder().model("gpt-4o-mini")
                                .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                                    ChatCompletionUserMessageParam.builder()
                                        .role(ChatCompletionUserMessageParam.Role.USER)
                                        .content(ChatCompletionUserMessageParam.Content.ofTextContent("private-prompt"))
                                        .build())).build());
                        assertThat(response.choices().get(0).message().content()).contains("private-answer");
                    });
            });
            assertThat(modelRequests).singleElement().asString().contains("private-prompt");
            assertThat(modelAuth).containsExactly("Bearer synthetic-openai-key");
            assertThat(received).isNotEmpty();
            assertThat(encodings).containsOnly(gzip ? "gzip" : "none");
            assertThat(contentTypes).containsOnly("application/x-protobuf");
            assertThat(apiKeys).containsOnly("synthetic-key");
            assertThat(secrets).containsOnly("synthetic-secret");
            var resources = received.stream().flatMap(r -> r.getResourceSpansList().stream()).toList();
            assertThat(resources).allSatisfy(r -> assertThat(r.getResource().getAttributesList()).anySatisfy(a -> {
                assertThat(a.getKey()).isEqualTo("project_name");
                assertThat(a.getValue().getStringValue()).isEqualTo("my-spring-app");
            }));
            var spans = resources.stream().flatMap(r -> r.getScopeSpansList().stream())
                .flatMap(s -> s.getSpansList().stream()).toList();
            var modelSpans = spans.stream().filter(s -> s.getName().equals("OpenAI Chat Completion")).toList();
            assertThat(modelSpans).hasSize(1);
            var modelSpan = modelSpans.get(0);
            assertThat(modelSpan.getAttributesList()).anySatisfy(a -> {
                assertThat(a.getKey()).isEqualTo("gen_ai.usage.total_tokens");
                assertThat(a.getValue().getIntValue()).isEqualTo(5);
            });
            if (starterOwned) {
                assertThat(spans).hasSize(1);
            } else {
                assertThat(spans).hasSize(2);
                var parent = spans.stream().filter(s -> s.getName().equals("native-parent")).findFirst().orElseThrow();
                assertThat(modelSpan.getTraceId()).isEqualTo(parent.getTraceId());
                assertThat(modelSpan.getParentSpanId()).isEqualTo(parent.getSpanId());
            }
            assertThat(received.toString()).doesNotContain("private-prompt", "private-answer");
            assertThat(TraceAI.isInitialized()).isFalse();
        } finally {
            server.stop(0);
        }
    }

    @Configuration(proxyBeanMethods = false)
    @EnableAutoConfiguration
    static class Application {
        @Bean
        OpenAIClient openAI(@Value("${OPENAI_API_KEY}") String apiKey,
                            @Value("${openai.base-url:https://api.openai.com/v1}") String baseUrl) {
            return OpenAIOkHttpClient.builder().apiKey(apiKey).baseUrl(baseUrl).build();
        }

        @Bean
        TracedOpenAIClient tracedOpenAI(FITracer tracer, OpenAIClient openAI) {
            return new TracedOpenAIClient(openAI, tracer);
        }
    }
}
