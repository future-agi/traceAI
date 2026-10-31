package ai.traceai.cohere;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.cohere.api.Cohere;
import com.cohere.api.core.ObjectMappers;
import com.cohere.api.core.RequestOptions;
import com.cohere.api.requests.ChatStreamRequest;
import com.cohere.api.resources.v2.requests.V2ChatRequest;
import com.cohere.api.resources.v2.requests.V2ChatStreamRequest;
import com.cohere.api.resources.v2.requests.V2EmbedRequest;
import com.cohere.api.resources.v2.requests.V2RerankRequest;
import com.cohere.api.resources.v2.types.V2ChatStreamResponse;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import okhttp3.Interceptor;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Protocol;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okio.Buffer;
import okio.BufferedSource;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.*;

class TracedCohereTransportTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    static final String CHAT_INPUT = "{\"model\":\"test-model\",\"messages\":[{\"role\":\"user\",\"content\":\"private-input\"}]}";
    static final String CHAT_RESPONSE = """
        {"id":"response-id","finish_reason":"COMPLETE","message":{"role":"assistant",
        "content":[{"type":"text","text":"private-output"}],
        "tool_calls":[{"id":"call-id","type":"function","function":{"name":"private-tool","arguments":"{}"}}]},
        "usage":{"tokens":{"input_tokens":3,"output_tokens":5}}}
        """;
    static final String V2_STREAM = sse("message-start", "{\"id\":\"response-id\",\"delta\":{\"message\":{\"role\":\"assistant\"}}}")
        + sse("content-delta", "{\"index\":0,\"delta\":{\"message\":{\"content\":{\"text\":\"private-output\"}}}}")
        + sse("message-end", "{\"delta\":{\"finish_reason\":\"COMPLETE\",\"usage\":{\"tokens\":{\"input_tokens\":3,\"output_tokens\":5}}}}");
    static final String V1_STREAM = """
        {"event_type":"stream-start","generation_id":"response-id"}
        {"event_type":"text-generation","text":"private-output"}
        {"event_type":"stream-end","finish_reason":"COMPLETE","response":{"text":"private-output","generation_id":"response-id","meta":{"tokens":{"input_tokens":3,"output_tokens":5}}}}
        """;

    @Test
    void tracesV2ChatThroughRealSdkAndPreservesOptions() throws Exception {
        FakeTransport transport = new FakeTransport(CHAT_RESPONSE);
        TracedCohereClient traced = traced(transport, TraceConfig.builder().build());
        var request = json(CHAT_INPUT, V2ChatRequest.class);
        var response = traced.v2().chat(request, RequestOptions.builder().addHeader("X-Test-Option", "kept").build());
        assertThat(response.getId()).isEqualTo("response-id");
        assertThat(response.getMessage().getToolCalls()).isPresent();
        assertThat(transport.request.url().encodedPath()).isEqualTo("/v2/chat");
        assertThat(transport.request.header("X-Test-Option")).isEqualTo("kept");
        assertThat(transport.sentBody).contains("private-input", "test-model");
        SpanData span = onlySpan();
        assertThat(transport.spanId).isEqualTo(span.getSpanId());
        assertThat(value(span, SemanticConventions.RAW_INPUT)).contains("\"role\":\"user\"");
        assertThat(value(span, SemanticConventions.RAW_OUTPUT)).contains("private-tool");
        assertThat(value(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("private-output");
        assertUsage(span);
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @Test
    void tracesV2EmbedAndRerankThroughRealSdk() throws Exception {
        FakeTransport transport = new FakeTransport("""
            {"id":"embedding-id","embeddings":{"float":[[0.1,0.2],[0.3,0.4]]},
             "meta":{"billed_units":{"input_tokens":4}}}
            """);
        var v2 = traced(transport, TraceConfig.builder().build()).v2();
        var embedded = v2.embed(json("""
            {"model":"embed-model","input_type":"search_document","texts":["private-a","private-b"]}
            """, V2EmbedRequest.class));
        assertThat(embedded.getEmbeddings().getFloat().orElseThrow()).hasSize(2);
        SpanData span = onlySpan();
        assertThat(value(span, SemanticConventions.FI_SPAN_KIND)).isEqualTo("EMBEDDING");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_VECTOR_COUNT))).isEqualTo(2L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS))).isEqualTo(2L);
        assertThat(transport.request.url().encodedPath()).isEqualTo("/v2/embed");
        transport.body = "{\"results\":[{\"index\":1,\"relevance_score\":0.9}]}";
        var reranked = v2.rerank(json("""
            {"model":"rerank-model","query":"private-query","documents":["private-a","private-b"],"top_n":1}
            """, V2RerankRequest.class), RequestOptions.builder().addHeader("X-Test-Option", "rerank").build());
        assertThat(reranked.getResults().get(0).getIndex()).isEqualTo(1);
        assertThat(transport.request.url().encodedPath()).isEqualTo("/v2/rerank");
        assertThat(transport.request.header("X-Test-Option")).isEqualTo("rerank");
        assertThat(otel.getSpans()).hasSize(2);
        span = otel.getSpans().get(1);
        assertThat(value(span, SemanticConventions.FI_SPAN_KIND)).isEqualTo("RERANKER");
        assertThat(value(span, SemanticConventions.GEN_AI_RERANKER_QUERY)).isEqualTo("private-query");
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void streamsUntilTerminalEventAndRestoresParent(boolean v2) throws Exception {
        FakeTransport transport = new FakeTransport(v2 ? V2_STREAM : V1_STREAM);
        var traced = traced(transport, TraceConfig.builder().build());
        Span parent = otel.getOpenTelemetry().getTracer("parent").spanBuilder("parent").startSpan();
        try (Scope ignored = parent.makeCurrent(); var stream = stream(traced, v2)) {
            assertThat(otel.getSpans()).isEmpty();
            assertThat(Span.current().getSpanContext()).isEqualTo(parent.getSpanContext());
            var iterator = stream.iterator();
            iterator.next();
            assertThat(otel.getSpans()).isEmpty();
            iterator.next();
            assertThat(otel.getSpans()).isEmpty();
            iterator.next(); // terminal event completes even without another hasNext()
            assertThat(transport.closed).isTrue();
            assertThat(iterator.hasNext()).isFalse();
            assertThatThrownBy(iterator::next).isInstanceOf(java.util.NoSuchElementException.class);
            SpanData span = onlySpan();
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(value(span, "cohere.stream.outcome")).isEqualTo("completed");
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(value(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("private-output");
            assertUsage(span);
            assertThat(Span.current().getSpanContext()).isEqualTo(parent.getSpanContext());
        } finally {
            parent.end();
        }
        assertThat(otel.getSpans()).hasSize(2);
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void earlyCloseEndsIncompleteSpanAndClosesTransportOnce(boolean v2) throws Exception {
        FakeTransport transport = new FakeTransport(v2 ? V2_STREAM : V1_STREAM);
        var stream = stream(traced(transport, TraceConfig.builder().build()), v2);
        stream.iterator().next();
        stream.close();
        stream.close();
        assertThat(transport.closed).isTrue();
        assertThat(value(onlySpan(), "cohere.stream.outcome")).isEqualTo("incomplete");
        assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void malformedStreamsDoNotProduceSuccessfulSpans(boolean v2) throws Exception {
        FakeTransport transport = new FakeTransport(v2 ? "event: content-delta\ndata: {not-json}\n\n" : "{not-json}\n");
        var stream = stream(traced(transport, TraceConfig.builder().hideErrorDetails(true).build()), v2);
        assertThatThrownBy(() -> stream.iterator().next()).isInstanceOf(RuntimeException.class);
        assertThat(transport.closed).isTrue();
        assertThat(onlySpan().getStatus().getDescription()).isEmpty();
        if (v2) {
            // Both supported SDKs swallow malformed SSE events and expose EOF.
            assertThat(value(onlySpan(), "cohere.stream.outcome")).isEqualTo("incomplete");
            assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
        } else {
            assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(onlySpan().getEvents()).hasSize(1);
            assertThat(onlySpan().getEvents().get(0).getAttributes().asMap())
                .containsOnlyKeys(AttributeKey.stringKey("exception.type"));
        }
        stream.close();
        assertThat(otel.getSpans()).hasSize(1);
    }

    @ParameterizedTest
    @ValueSource(strings = {"chat", "embed", "rerank", "v1stream", "v2stream"})
    void transportErrorsPreserveSdkException(String operation) throws Exception {
        FakeTransport transport = new FakeTransport("{\"message\":\"private-error\"}");
        transport.code = 401;
        var traced = traced(transport, TraceConfig.builder().hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
        assertThatThrownBy(() -> {
            switch (operation) {
                case "chat": traced.v2().chat(json(CHAT_INPUT, V2ChatRequest.class)); break;
                case "embed": traced.v2().embed(json("{\"model\":\"m\",\"input_type\":\"search_query\",\"texts\":[\"private-input\"]}", V2EmbedRequest.class)); break;
                case "rerank": traced.v2().rerank(json("{\"model\":\"m\",\"query\":\"private-input\",\"documents\":[\"d\"]}", V2RerankRequest.class)); break;
                default: stream(traced, operation.equals("v2stream"));
            }
        }).isInstanceOf(com.cohere.api.errors.UnauthorizedError.class);
        assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(onlySpan().getAttributes().toString()).doesNotContain("private-");
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @ParameterizedTest
    @ValueSource(ints = {1, 2, 4, 8, 15})
    void newChatSurfacesRespectContentAndMessagePrivacy(int flags) throws Exception {
        TraceConfig config = TraceConfig.builder().hideInputs((flags & 1) != 0).hideOutputs((flags & 2) != 0)
            .hideInputMessages((flags & 4) != 0).hideOutputMessages((flags & 8) != 0).build();
        FakeTransport transport = new FakeTransport(CHAT_RESPONSE);
        var traced = traced(transport, config);
        traced.v2().chat(json(CHAT_INPUT, V2ChatRequest.class));
        transport.body = V2_STREAM;
        try (var stream = stream(traced, true)) { stream.forEach(event -> {}); }
        transport.body = V1_STREAM;
        try (var stream = stream(traced, false)) { stream.forEach(event -> {}); }
        assertThat(otel.getSpans()).hasSize(3);
        for (SpanData span : otel.getSpans()) {
            if ((flags & 5) != 0) assertThat(span.getAttributes().toString()).doesNotContain("private-input");
            if ((flags & 10) != 0) assertThat(span.getAttributes().toString()).doesNotContain("private-output", "private-tool");
            assertUsage(span);
        }
    }

    @Test
    void terminalProviderErrorIsNotReportedAsSuccess() throws Exception {
        FakeTransport transport = new FakeTransport(sse("message-end",
            "{\"delta\":{\"finish_reason\":\"ERROR\",\"error\":\"private-error\"}}"));
        try (var stream = stream(traced(transport, TraceConfig.builder().hideErrorDetails(true).build()), true)) {
            stream.forEach(event -> {});
        }
        assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(value(onlySpan(), "cohere.stream.outcome")).isEqualTo("error");
        assertThat(onlySpan().getAttributes().toString()).doesNotContain("private-error");
    }

    @Test
    void synchronousProviderErrorIsNotReportedAsSuccess() throws Exception {
        FakeTransport transport = new FakeTransport(CHAT_RESPONSE.replace("COMPLETE", "ERROR"));
        var traced = traced(transport, TraceConfig.builder().hideErrorDetails(true).build());
        var response = traced.v2().chat(json(CHAT_INPUT, V2ChatRequest.class));
        assertThat(response.getFinishReason().toString()).isEqualTo("ERROR");
        assertThat(onlySpan().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void absentUsageDoesNotInventZeroCounts() throws Exception {
        FakeTransport transport = new FakeTransport("{\"id\":\"r\",\"finish_reason\":\"COMPLETE\",\"message\":{\"role\":\"assistant\"}}");
        traced(transport, TraceConfig.builder().build()).v2().chat(json(CHAT_INPUT, V2ChatRequest.class));
        assertThat(onlySpan().getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isNull();
    }

    @Test
    void unwrapRemainsOriginalUntracedEscapeHatch() throws Exception {
        FakeTransport transport = new FakeTransport(CHAT_RESPONSE);
        var traced = traced(transport, TraceConfig.builder().build());
        traced.unwrap().v2().chat(json(CHAT_INPUT, V2ChatRequest.class));
        assertThat(otel.getSpans()).isEmpty();
    }

    static TracedCohereStream<?> stream(TracedCohereClient traced, boolean v2) throws IOException {
        if (v2) return traced.v2().chatStream(json(CHAT_INPUT, V2ChatStreamRequest.class));
        return traced.chatStream(ChatStreamRequest.builder().message("private-input").model("test-model").build());
    }

    private TracedCohereClient traced(FakeTransport transport, TraceConfig config) {
        return new TracedCohereClient(transport.client(), new FITracer(otel.getOpenTelemetry().getTracer("cohere-test"), config));
    }

    static <T> T json(String value, Class<T> type) throws IOException {
        return ObjectMappers.JSON_MAPPER.readValue(value, type);
    }

    static String sse(String type, String data) {
        return "event: " + type + "\ndata: {\"type\":\"" + type + "\"," + data.substring(1) + "\n\n";
    }

    private SpanData onlySpan() {
        assertThat(otel.getSpans()).hasSize(1);
        return otel.getSpans().get(0);
    }

    static String value(SpanData span, String key) {
        return span.getAttributes().get(AttributeKey.stringKey(key));
    }

    private void assertUsage(SpanData span) {
        assertThat(value(span, SemanticConventions.LLM_PROVIDER)).isEqualTo("cohere");
        assertThat(value(span, SemanticConventions.LLM_REQUEST_MODEL)).isEqualTo("test-model");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT))).isEqualTo(3L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION))).isEqualTo(5L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(8L);
    }

    /** All provider traffic ends here: no DNS, sockets, credentials or vendor API calls. */
    static final class FakeTransport implements Interceptor {
        String body;
        int code = 200;
        Request request;
        String sentBody;
        String spanId;
        boolean closed;

        FakeTransport(String body) { this.body = body; }

        Cohere client() {
            return Cohere.builder().token("synthetic-not-a-credential")
                .httpClient(new OkHttpClient.Builder().addInterceptor(this).build()).build();
        }

        @Override
        public Response intercept(Chain chain) throws IOException {
            request = chain.request();
            Buffer sent = new Buffer();
            request.body().writeTo(sent);
            sentBody = sent.readUtf8();
            spanId = Span.current().getSpanContext().getSpanId();
            Buffer buffer = new Buffer().writeUtf8(body);
            ResponseBody responseBody = new ResponseBody() {
                @Override public MediaType contentType() { return MediaType.parse("application/json"); }
                @Override public long contentLength() { return -1; }
                @Override public BufferedSource source() { return buffer; }
                @Override public void close() { closed = true; super.close(); }
            };
            return new Response.Builder().request(request).protocol(Protocol.HTTP_1_1)
                .code(code).message("synthetic").body(responseBody).build();
        }
    }
}
