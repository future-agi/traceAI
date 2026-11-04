package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClientImpl;
import com.openai.client.okhttp.OkHttpClient;
import com.openai.core.ClientOptions;
import com.openai.models.ChatCompletionCreateParams;
import com.openai.models.ChatCompletionMessageParam;
import com.openai.models.ChatCompletionUserMessageParam;
import com.openai.models.EmbeddingCreateParams;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import java.util.concurrent.TimeUnit;
import static ai.traceai.openai.TracingHttpClientTest.attr;
import static org.assertj.core.api.Assertions.assertThat;

class LegacyPrivacyTest {
    private static final String AUTH = "synthetic-per-call-auth";
    private static final String KEY = "synthetic-per-call-key";
    private static final String QUERY = "synthetic-query-credential";

    @ParameterizedTest
    @ValueSource(strings = {"chat", "stream", "embedding"})
    void perCallTransportCredentialsAreNotExported(String operation) throws Exception {
        SpanData span = call(operation, TraceConfig.builder().build());
        assertThat(span.toString()).doesNotContain(AUTH, KEY, QUERY, "additionalHeaders", "additionalQueryParams");
        // Do not satisfy the credential policy by disabling all request-body telemetry.
        assertThat(attr(span, "fi.raw_input")).contains("private-question", "requested-model");
    }

    @ParameterizedTest
    @CsvSource({"chat,messages", "stream,messages", "embedding,messages",
        "chat,all", "stream,all", "embedding,all", "chat,input", "stream,input", "embedding,input",
        "chat,output", "stream,output", "embedding,output"})
    void privacyFlagsCoverTheEntireExportedSpan(String operation, String hiding) throws Exception {
        boolean all = hiding.equals("all");
        boolean hideInput = !hiding.equals("output");
        boolean hideOutput = !hiding.equals("input");
        var config = TraceConfig.builder().hideInputs(all).hideOutputs(all)
            .hideInputMessages(!all && hideInput).hideOutputMessages(!all && hideOutput).build();
        SpanData span = call(operation, config);
        assertThat(span.toString()).doesNotContain(AUTH, KEY, QUERY);
        if (hideInput) assertThat(span.toString()).doesNotContain("private-question");
        else assertThat(attr(span, "fi.raw_input")).contains("private-question");
        if (hideOutput) assertThat(span.toString()).doesNotContain("private-answer");
        else if (!operation.equals("embedding")) assertThat(attr(span, "output.value")).isEqualTo("private-answer");
        assertThat(attr(span, "gen_ai.operation.name")).isEqualTo(operation.equals("embedding") ? "embeddings" : "chat");
    }

    @ParameterizedTest
    @ValueSource(strings = {"chat", "stream"})
    void nestedUsageIsRecordedWithLegacyContentHidden(String operation) throws Exception {
        SpanData span = call(operation, TraceConfig.builder().hideInputs(true).hideOutputs(true).build());
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey("gen_ai.usage.input_tokens.cache_read")))
            .isEqualTo(6);
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey("gen_ai.usage.output_tokens.reasoning")))
            .isEqualTo(2);
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey("gen_ai.usage.total_tokens"))).isEqualTo(15);
    }

    private SpanData call(String operation, TraceConfig config) throws Exception {
        String reply = operation.equals("stream") ? OpenAILoopback.CHAT_SSE
            : operation.equals("embedding") ? OpenAILoopback.EMBEDDING : OpenAILoopback.CHAT;
        var exporter = InMemorySpanExporter.create();
        try (var server = new OpenAILoopback(operation.equals("stream") ? "text/event-stream" : "application/json", reply);
             var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracer = new FITracer(provider.get("legacy-privacy"), config);
            var transport = TracingHttpClient.wrap(OkHttpClient.builder().baseUrl(server.url()).build(), tracer);
            var client = new OpenAIClientImpl(ClientOptions.builder().httpClient(transport)
                .baseUrl(server.url()).apiKey("synthetic-client-key").maxRetries(0).build());
            try (transport) {
                var legacy = new TracedOpenAIClient(client, tracer);
                if (operation.equals("embedding")) {
                    var params = EmbeddingCreateParams.builder().model("requested-model")
                        .input(EmbeddingCreateParams.Input.ofString("private-question"))
                        .putAdditionalHeader("Authorization", "Bearer " + AUTH).putAdditionalHeader("X-API-Key", KEY)
                        .putAdditionalQueryParam("api_key", QUERY).build();
                    assertThat(legacy.createEmbedding(params).data().get(0).embedding()).hasSize(2);
                } else {
                    var params = ChatCompletionCreateParams.builder().model("requested-model")
                        .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                            ChatCompletionUserMessageParam.builder().content(
                                ChatCompletionUserMessageParam.Content.ofTextContent("private-question")).build()))
                        .putAdditionalHeader("Authorization", "Bearer " + AUTH).putAdditionalHeader("X-API-Key", KEY)
                        .putAdditionalQueryParam("api_key", QUERY).build();
                    if (operation.equals("stream")) {
                        try (var response = legacy.streamChatCompletionResponse(params)) {
                            var chunks = response.stream().toList();
                            assertThat(chunks).hasSize(2);
                            assertThat(chunks.get(0).choices().get(0).delta().content()).contains("private-answer");
                        }
                    } else {
                        assertThat(legacy.createChatCompletion(params).choices().get(0).message().content()).contains("private-answer");
                    }
                }
                assertThat(server.requests.poll(5, TimeUnit.SECONDS)).contains(AUTH, KEY, QUERY, "private-question");
                assertThat(exporter.getFinishedSpanItems()).hasSize(1);
                return exporter.getFinishedSpanItems().get(0);
            }
        }
    }
}
