package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClientImpl;
import com.openai.client.okhttp.OkHttpClient;
import com.openai.core.ClientOptions;
import com.google.gson.JsonParser;
import io.opentelemetry.api.common.AttributeKey;
import org.junit.jupiter.api.Test;
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

    @ParameterizedTest @ValueSource(strings = {"chat", "embedding"})
    void rawOutputContainsResponseDataInsteadOfSdkInternals(String operation) throws Exception {
        var span = call(operation, TraceConfig.builder().build());
        String raw = attr(span, "fi.raw_output");
        assertThat(raw).doesNotContain("hashCode$delegate", "additionalProperties", "validated");
        var json = JsonParser.parseString(raw).getAsJsonObject();
        assertThat(json.get("model").getAsString()).isEqualTo("served-model");
        assertThat(json.getAsJsonObject("usage").get("total_tokens").getAsInt()).isEqualTo(operation.equals("chat") ? 15 : 10);
        if (operation.equals("chat")) {
            assertThat(json.get("id").getAsString()).isEqualTo("chat-test");
            assertThat(json.getAsJsonArray("choices").get(0).getAsJsonObject().getAsJsonObject("message").get("content").getAsString())
                .isEqualTo("private-answer");
        } else {
            var values = json.getAsJsonArray("data").get(0).getAsJsonObject().getAsJsonArray("embedding");
            assertThat(values).hasSize(2);
            assertThat(values.get(0).getAsDouble()).isEqualTo(0.1);
            assertThat(values.get(1).getAsDouble()).isEqualTo(0.2);
        }
    }

    @Test void rawOutputContainsToolNamesAndArguments() throws Exception {
        String reply = OpenAILoopback.CHAT.replace("\"content\":\"private-answer\"", "\"content\":\"private-answer\","
            + "\"tool_calls\":[{\"type\":\"function\",\"id\":\"call-test\",\"function\":{\"name\":\"lookup\",\"arguments\":\"{\\\"city\\\":\\\"Paris\\\"}\"}}]");
        var span = call("chat", TraceConfig.builder().build(), reply);
        var tool = JsonParser.parseString(attr(span, "fi.raw_output")).getAsJsonObject().getAsJsonArray("choices")
            .get(0).getAsJsonObject().getAsJsonObject("message").getAsJsonArray("tool_calls").get(0).getAsJsonObject();
        assertThat(tool.get("id").getAsString()).isEqualTo("call-test");
        assertThat(tool.getAsJsonObject("function").get("name").getAsString()).isEqualTo("lookup");
        assertThat(tool.getAsJsonObject("function").get("arguments").getAsString()).isEqualTo("{\"city\":\"Paris\"}");
    }

    @ParameterizedTest @CsvSource({"chat,false", "chat,true", "embedding,false", "embedding,true"})
    void rawOutputHonorsBothOutputHidingFlags(String operation, boolean messages) throws Exception {
        var span = call(operation, TraceConfig.builder().hideOutputs(!messages).hideOutputMessages(messages).build());
        assertThat(attr(span, "fi.raw_output")).isNull();
    }

    @Test void oversizedRawOutputIsOmittedWithoutChangingTheSdkResponse() throws Exception {
        String reply = OpenAILoopback.CHAT.replace("\"model\":\"served-model\"", "\"model\":\"served-model\",\"padding\":\"" + "x".repeat(70_000) + "\"");
        var span = call("chat", TraceConfig.builder().build(), reply);
        assertThat(attr(span, "fi.raw_output")).isNull();
        assertThat(span.getAttributes().get(AttributeKey.booleanKey("traceai.capture.truncated"))).isTrue();
    }

    @Test void rawOutputSerializationFailureIsFailOpenAndNeverFallsBackToToString() {
        var exporter = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            var tracer = new FITracer(provider.get("serialization"));
            var span = tracer.startSpan("serialization", ai.traceai.FISpanKind.LLM);
            LegacyOpenAITrace.output(tracer, span, new UnserializableResponse());
            span.end();
            var data = exporter.getFinishedSpanItems().get(0);
            assertThat(data.getAttributes().get(AttributeKey.booleanKey("traceai.capture.skipped"))).isTrue();
            assertThat(attr(data, "fi.raw_output")).isNull();
            assertThat(data.toString()).doesNotContain("private-serialization-error", "sdk-internals");
        }
    }

    public static class UnserializableResponse {
        public String getContent() { throw new IllegalStateException("private-serialization-error"); }
        @Override public String toString() { return "sdk-internals"; }
    }

    private SpanData call(String operation, TraceConfig config) throws Exception {
        String reply = operation.equals("stream") ? OpenAILoopback.CHAT_SSE
            : operation.equals("embedding") ? OpenAILoopback.EMBEDDING : OpenAILoopback.CHAT;
        return call(operation, config, reply);
    }

    private SpanData call(String operation, TraceConfig config, String reply) throws Exception {
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
