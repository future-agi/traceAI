package ai.traceai.openai;

import ai.traceai.ContextAttributes;
import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClientImpl;
import com.openai.client.okhttp.OkHttpClient;
import com.openai.core.ClientOptions;
import com.openai.core.http.HttpClient;
import com.openai.models.chat.completions.ChatCompletionCreateParams;
import com.openai.models.responses.ResponseCreateParams;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import static ai.traceai.openai.TracingHttpClientTest.attr;
import static org.assertj.core.api.Assertions.assertThat;

class ModernPrivacyContextUsageTest {
    ChatCompletionCreateParams chat() {
        return ChatCompletionCreateParams.builder().model("requested-model").addUserMessage("private-question").build();
    }

    @Test void nativeAsyncKeepsCallerIdentityAndParent() throws Exception {
        var exporter = InMemorySpanExporter.create();
        try (var server = new OpenAILoopback("application/json", OpenAILoopback.CHAT);
             var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var transport = TracingHttpClient.wrap(OkHttpClient.builder().build(), new FITracer(provider.get("context")));
            var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(transport)
                .baseUrl(server.url()).apiKey("synthetic-key").maxRetries(0).build());
            var parent = provider.get("context").spanBuilder("parent").startSpan();
            try (AutoCloseable close = client::close; var scope = parent.makeCurrent();
                 var user = ContextAttributes.usingUser("caller-user");
                 var session = ContextAttributes.usingSession("caller-session");
                 var metadata = ContextAttributes.usingMetadata(Map.of("tenant", "caller-tenant"));
                 var tags = ContextAttributes.usingTags(List.of("caller-tag"))) {
                var view = client.withOptions(o -> o.httpClient(TracingHttpClient.inCurrentContext(transport)));
                assertThat(view.async().chat().completions().create(chat()).get(10, TimeUnit.SECONDS).id()).isEqualTo("chat-test");
                assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                var span = exporter.getFinishedSpanItems().get(0);
                assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
                assertThat(attr(span, "user.id")).isEqualTo("caller-user");
                assertThat(attr(span, "session.id")).isEqualTo("caller-session");
                assertThat(span.getAttributes().toString()).contains("caller-tenant", "caller-tag");
            } finally { parent.end(); }
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void callerSnapshotNeverInheritsWorkerIdentityEvenWhenEmpty(boolean hasCaller) throws Exception {
        var exporter = InMemorySpanExporter.create();
        try (var server = new OpenAILoopback("application/json", OpenAILoopback.CHAT);
             var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var transport = TracingHttpClient.wrap(OkHttpClient.builder().build(), new FITracer(provider.get("context")));
            var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(transport)
                .baseUrl(server.url()).apiKey("synthetic-key").maxRetries(0).build());
            HttpClient bound;
            try (var user = ContextAttributes.usingUser(hasCaller ? "caller-user" : null)) {
                bound = TracingHttpClient.inCurrentContext(transport);
            }
            var executor = Executors.newSingleThreadExecutor();
            try (AutoCloseable close = client::close) {
                executor.submit(() -> {
                    try (var user = ContextAttributes.usingUser("worker-user");
                         var session = ContextAttributes.usingSession("worker-session");
                         var metadata = ContextAttributes.usingMetadata(Map.of("tenant", "worker-tenant"));
                         var tags = ContextAttributes.usingTags(List.of("worker-tag"))) {
                        var before = ContextAttributes.getAttributesFromContext();
                        client.withOptions(o -> o.httpClient(bound)).chat().completions().create(chat());
                        assertThat(ContextAttributes.getAttributesFromContext()).isEqualTo(before);
                        // A fresh view of a bound transport must capture this caller, not the old one.
                        client.withOptions(o -> o.httpClient(TracingHttpClient.inCurrentContext(bound)))
                            .chat().completions().create(chat());
                    }
                    return null;
                }).get(10, TimeUnit.SECONDS);
                assertThat(exporter.getFinishedSpanItems()).hasSize(2);
                var span = exporter.getFinishedSpanItems().get(0);
                assertThat(attr(span, "user.id")).isEqualTo(hasCaller ? "caller-user" : null);
                assertThat(span.toString()).doesNotContain("worker-user", "worker-session", "worker-tenant", "worker-tag");
                assertThat(attr(exporter.getFinishedSpanItems().get(1), "user.id")).isEqualTo("worker-user");
            } finally { executor.shutdownNow(); }
        }
    }

    @ParameterizedTest
    @CsvSource({"chat,false,all", "chat,true,all", "responses,false,all", "responses,true,all",
        "chat,false,messages", "chat,true,messages", "responses,false,messages", "responses,true,messages"})
    void nestedUsageSurvivesPrivacyInJsonAndSse(String operation, boolean streaming, String hiding) throws Exception {
        boolean responses = operation.equals("responses");
        String reply = responses ? (streaming ? OpenAILoopback.RESPONSE_SSE : OpenAILoopback.RESPONSE)
            : (streaming ? OpenAILoopback.CHAT_SSE : OpenAILoopback.CHAT);
        boolean all = hiding.equals("all");
        var config = TraceConfig.builder().hideInputs(all).hideOutputs(all)
            .hideInputMessages(!all).hideOutputMessages(!all).build();
        var exporter = InMemorySpanExporter.create();
        try (var server = new OpenAILoopback(streaming ? "text/event-stream" : "application/json", reply);
             var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var client = new OpenAIClientImpl(ClientOptions.builder()
                .httpClient(TracingHttpClient.wrap(OkHttpClient.builder().build(), new FITracer(provider.get("usage"), config)))
                .baseUrl(server.url()).apiKey("synthetic-key").maxRetries(0).build());
            try (AutoCloseable close = client::close) {
                if (responses) {
                    var params = ResponseCreateParams.builder().model("requested-model").input("private-question").build();
                    if (streaming) {
                        try (var stream = client.responses().createStreaming(params)) { assertThat(stream.stream().count()).isEqualTo(1); }
                    } else { assertThat(client.responses().create(params).id()).isEqualTo("resp-test"); }
                } else if (streaming) {
                    try (var stream = client.chat().completions().createStreaming(chat())) { assertThat(stream.stream().count()).isEqualTo(2); }
                } else { assertThat(client.chat().completions().create(chat()).id()).isEqualTo("chat-test"); }
                assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                var span = exporter.getFinishedSpanItems().get(0);
                assertThat(span.toString()).doesNotContain("private-question", "private-answer", "synthetic-key");
                assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens"))).isEqualTo(10);
                assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.output_tokens"))).isEqualTo(5);
                assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.total_tokens"))).isEqualTo(15);
                assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens.cache_read"))).isEqualTo(6);
                assertThat(span.getAttributes().get(AttributeKey.longKey("gen_ai.usage.output_tokens.reasoning"))).isEqualTo(2);
            }
        }
    }
}
