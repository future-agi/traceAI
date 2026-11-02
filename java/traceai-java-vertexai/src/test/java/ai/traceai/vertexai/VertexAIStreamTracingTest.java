package ai.traceai.vertexai;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.google.cloud.vertexai.api.Candidate;
import com.google.cloud.vertexai.api.Content;
import com.google.cloud.vertexai.api.CountTokensResponse;
import com.google.cloud.vertexai.api.GenerateContentResponse;
import com.google.cloud.vertexai.api.Part;
import com.google.cloud.vertexai.generativeai.GenerativeModel;
import com.google.cloud.vertexai.generativeai.ResponseStream;
import com.google.gson.JsonParser;
import io.grpc.Status;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import com.google.cloud.vertexai.generativeai.ResponseStreamIteratorWithHistory;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.Iterator;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** Drives the released Vertex AI SDK against an in-process gRPC service. */
class VertexAIStreamTracingTest {
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private FakeVertexService service;

    @BeforeEach
    void setUp() throws Exception {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        service = new FakeVertexService();
    }

    @AfterEach
    void tearDown() {
        service.close();
        provider.close();
    }

    private TracedGenerativeModel model(TraceConfig config) {
        return new TracedGenerativeModel(
            new GenerativeModel("gemini-test", service.vertexAI),
            new FITracer(provider.get("test"), config));
    }

    private TracedGenerativeModel model() {
        return model(TraceConfig.builder().build());
    }

    static GenerateContentResponse chunk(String text) {
        return GenerateContentResponse.newBuilder()
            .addCandidates(Candidate.newBuilder()
                .setContent(Content.newBuilder().setRole("model").addParts(Part.newBuilder().setText(text))))
            .build();
    }

    static GenerateContentResponse lastChunk(String text) {
        return GenerateContentResponse.newBuilder()
            .addCandidates(Candidate.newBuilder()
                .setContent(Content.newBuilder().setRole("model").addParts(Part.newBuilder().setText(text)))
                .setFinishReason(Candidate.FinishReason.STOP))
            .setUsageMetadata(GenerateContentResponse.UsageMetadata.newBuilder()
                .setPromptTokenCount(3).setCandidatesTokenCount(2).setTotalTokenCount(5))
            .build();
    }

    private static String attr(SpanData span, String key) {
        return span.getAttributes().get(AttributeKey.stringKey(key));
    }

    private static Long longAttr(SpanData span, String key) {
        return span.getAttributes().get(AttributeKey.longKey(key));
    }

    @Test
    void streamSpanStaysOpenUntilTheLastChunkAndRecordsTheAnswer() throws Exception {
        service.streamChunks = List.of(chunk("Hel"), lastChunk("lo"));

        ResponseStream<GenerateContentResponse> stream = model().generateContentStream("Say hello");
        assertThat(exporter.getFinishedSpanItems()).isEmpty();

        StringBuilder seen = new StringBuilder();
        for (GenerateContentResponse response : stream) {
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            seen.append(response.getCandidates(0).getContent().getParts(0).getText());
        }

        assertThat(seen.toString()).isEqualTo("Hello");
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getName()).isEqualTo("Vertex AI Generate Content (Stream)");
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(attr(span, SemanticConventions.INPUT_VALUE)).isEqualTo("Say hello");
        assertThat(attr(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("Hello");
        assertThat(attr(span, SemanticConventions.LLM_OUTPUT_MESSAGES)).contains("Hello");
        assertThat(span.getAttributes().get(AttributeKey.stringArrayKey("gen_ai.response.finish_reasons")))
            .containsExactly("STOP");
        assertThat(longAttr(span, SemanticConventions.LLM_TOKEN_COUNT_PROMPT)).isEqualTo(3L);
        assertThat(longAttr(span, SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(2L);
        assertThat(longAttr(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(5L);
        assertThat(attr(span, SemanticConventions.LLM_PROVIDER)).isEqualTo("google");
        assertThat(attr(span, SemanticConventions.LLM_REQUEST_MODEL)).isEqualTo("gemini-test");
        assertThat(JsonParser.parseString(attr(span, SemanticConventions.RAW_OUTPUT)).isJsonArray()).isTrue();
    }

    @Test
    void streamFailureDuringIterationEndsTheSpanAsAnError() throws Exception {
        service.streamChunks = List.of(chunk("Hel"));
        service.streamError = Status.UNAVAILABLE.withDescription("synthetic outage");

        ResponseStream<GenerateContentResponse> stream = model().generateContentStream("Say hello");
        Iterator<GenerateContentResponse> iterator = stream.iterator();
        assertThat(iterator.next().getCandidates(0).getContent().getParts(0).getText()).isEqualTo("Hel");
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        assertThatThrownBy(() -> {
            while (iterator.hasNext()) iterator.next();
        }).isInstanceOf(RuntimeException.class);

        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(attr(span, SemanticConventions.ERROR_TYPE)).isNotBlank();
        assertThat(attr(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("Hel");
    }

    @Test
    void everySpanReportsTheSameProvider() throws Exception {
        service.unaryResponse = lastChunk("Hello");
        service.countResponse = CountTokensResponse.newBuilder().setTotalTokens(4).build();
        TracedGenerativeModel traced = model();

        traced.generateContent("Say hello");
        traced.countTokens("Say hello");

        assertThat(exporter.getFinishedSpanItems()).hasSize(2)
            .allSatisfy(span -> assertThat(attr(span, SemanticConventions.LLM_PROVIDER)).isEqualTo("google"));
    }

    @Test
    void replayAndStreamApiKeepSdkBehaviorWithoutDuplicateSpans() throws Exception {
        service.streamChunks = List.of(chunk("Hel"), lastChunk("lo"));
        ResponseStream<GenerateContentResponse> response = model().generateContentStream("hello");
        assertThat(response.isConsumed()).isFalse();
        assertThat(response.stream().count()).isEqualTo(2);
        assertThat(response.isConsumed()).isTrue();
        assertThat(response.stream().count()).isEqualTo(2);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(attr(exporter.getFinishedSpanItems().get(0), SemanticConventions.OUTPUT_VALUE)).isEqualTo("Hello");
    }

    @Test
    void closeAfterShortCircuitEndsIncompleteSpanOnce() throws Exception {
        service.streamChunks = List.of(chunk("Hel"), lastChunk("lo"));
        try (var stream = model().generateContentStream("hello").stream()) {
            assertThat(stream.findFirst()).isPresent();
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
        assertThat(span.getAttributes().get(AttributeKey.booleanKey("fi.stream.incomplete"))).isTrue();
        assertThat(attr(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("Hel");
    }

    @Test
    void closeWithoutConsumptionEndsOnceWithoutReadingTheStream() throws Exception {
        service.streamChunks = List.of(lastChunk("answer"));
        var stream = model().generateContentStream("hello").stream();
        stream.close();
        stream.close();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.UNSET);
    }

    @Test
    void metadataOnlyLastChunkDoesNotEraseAnswer() throws Exception {
        service.streamChunks = List.of(chunk("answer"), lastChunk("").toBuilder().clearCandidates().build());
        model().generateContentStream("hello").stream().count();
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(attr(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("answer");
        assertThat(longAttr(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(5L);
    }

    @Test
    void invalidStreamInputClosesTheSetupSpan() {
        assertThatThrownBy(() -> model().generateContentStream((String) null)).isInstanceOf(RuntimeException.class);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @Test
    void hiddenUnaryContentDoesNotSerializeProtobufInputsOrOutputs() throws Exception {
        service.unaryResponse = lastChunk("private answer");
        var content = Content.newBuilder().setRole("user").addParts(Part.newBuilder().setText("private prompt")).build();
        model(TraceConfig.builder().hideInputs(true).hideOutputs(true).build()).generateContent(List.of(content));
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(attr(span, SemanticConventions.RAW_INPUT)).isNull();
        assertThat(attr(span, SemanticConventions.RAW_OUTPUT)).isNull();
        assertThat(span.getAttributes().toString()).doesNotContain("private");
        assertThat(longAttr(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(5L);
    }

    @Test
    void emptyStreamEndsSuccessfully() throws Exception {
        assertThat(model().generateContentStream("hello").stream().count()).isZero();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
    }

    @Test
    void failureBeforeAnyChunkIsNotSuccess() throws Exception {
        service.streamError = Status.INVALID_ARGUMENT.withDescription("synthetic error");
        assertThatThrownBy(() -> model().generateContentStream("hello").stream().count())
            .isInstanceOf(RuntimeException.class);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @Test
    void hiddenContentAndErrorsStayHiddenWhileUsageSurvives() throws Exception {
        service.streamChunks = List.of(lastChunk("private answer"));
        service.streamError = Status.INVALID_ARGUMENT.withDescription("private diagnostic");
        TraceConfig config = TraceConfig.builder().hideInputs(true).hideOutputs(true).hideErrorDetails(true).build();
        assertThatThrownBy(() -> model(config).generateContentStream("private prompt").stream().count())
            .isInstanceOf(RuntimeException.class);
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(attr(span, SemanticConventions.INPUT_VALUE)).isNull();
        assertThat(attr(span, SemanticConventions.OUTPUT_VALUE)).isNull();
        assertThat(attr(span, SemanticConventions.RAW_OUTPUT)).isNull();
        assertThat(attr(span, SemanticConventions.LLM_INPUT_MESSAGES)).isNull();
        assertThat(attr(span, SemanticConventions.LLM_OUTPUT_MESSAGES)).isNull();
        assertThat(span.getAttributes().toString()).doesNotContain("private");
        assertThat(span.getEvents()).allSatisfy(event -> {
            assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.type"))).isNotBlank();
            assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.message"))).isNull();
            assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.stacktrace"))).isNull();
        });
        assertThat(span.getStatus().getDescription()).doesNotContain("private");
        assertThat(longAttr(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(5L);
    }

    @Test
    void messageHideFlagDoesNotHideOutputValue() throws Exception {
        service.streamChunks = List.of(lastChunk("answer"));
        model(TraceConfig.builder().hideOutputMessages(true).build()).generateContentStream("hello").stream().count();
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(attr(span, SemanticConventions.LLM_OUTPUT_MESSAGES)).isNull();
        assertThat(attr(span, SemanticConventions.OUTPUT_VALUE)).isEqualTo("answer");
    }

    @Test
    void streamingDoesNotLeakScopeAndKeepsOriginalParent() throws Exception {
        service.streamChunks = List.of(lastChunk("answer"));
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        ResponseStream<GenerateContentResponse> response;
        try (Scope ignored = parent.makeCurrent()) {
            response = model().generateContentStream("hello");
            assertThat(Span.current()).isSameAs(parent);
        }
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
        response.stream().count();
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
        assertThat(exporter.getFinishedSpanItems().get(0).getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
        parent.end();
    }

    @Test
    void nextFailureIsRethrownUnchangedAndEndsExactlyOnce() {
        RuntimeException failure = new IllegalStateException("synthetic-next-failure");
        Iterator<GenerateContentResponse> iterator = new Iterator<>() {
            public boolean hasNext() { return true; }
            public GenerateContentResponse next() { throw failure; }
        };
        Span span = provider.get("test").spanBuilder("stream").startSpan();
        var source = new ResponseStream<GenerateContentResponse>(new ResponseStreamIteratorWithHistory<>(iterator));
        var stream = TracedResponseStream.wrap(source, span, new FITracer(provider.get("test"))).stream();
        assertThatThrownBy(() -> stream.forEach(ignored -> {})).isSameAs(failure);
        stream.close();
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
    }

    @ParameterizedTest
    @ValueSource(strings = {"a", "\"\\\n\t", "\uD83D\uDE80"})
    void largeStreamBoundsTelemetryWithoutChangingResponses(String pattern) throws Exception {
        String text = pattern.repeat(20000 / pattern.length());
        service.streamChunks = List.of(chunk(text), lastChunk(text));
        assertThat(model().generateContentStream("hello").stream()
            .mapToInt(r -> r.getCandidates(0).getContent().getParts(0).getText().length()).sum()).isEqualTo(40000);
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(attr(span, SemanticConventions.OUTPUT_VALUE)).hasSize(32000);
        String messages = attr(span, "gen_ai.output.messages");
        assertThat(messages.length()).isLessThanOrEqualTo(32000);
        var message = JsonParser.parseString(messages).getAsJsonArray().get(0).getAsJsonObject();
        assertThat(message.get("role").getAsString()).isEqualTo("model");
        String content = message.get("content").getAsString();
        assertThat(content).isNotEmpty().endsWith("...[truncated]");
        assertThat(text + text).startsWith(content.substring(0, content.length() - "...[truncated]".length()));
        assertThat(attr(span, SemanticConventions.RAW_OUTPUT).length()).isLessThanOrEqualTo(32000);
        assertThat(JsonParser.parseString(attr(span, SemanticConventions.RAW_OUTPUT)).isJsonArray()).isTrue();
        assertThat(span.getAttributes().get(AttributeKey.booleanKey("fi.stream.raw_output_truncated"))).isTrue();
        assertThat(longAttr(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(5L);
    }

    @Test
    void unaryErrorKeepsErrorStatusAndHiddenDetails() {
        service.unaryError = Status.INVALID_ARGUMENT.withDescription("private diagnostic");
        assertThatThrownBy(() -> model(TraceConfig.builder().hideErrorDetails(true).build()).generateContent("hello"))
            .isInstanceOf(RuntimeException.class);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        SpanData span = exporter.getFinishedSpanItems().get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(span.getEvents()).allSatisfy(event -> {
            assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.type"))).isNotBlank();
            assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.message"))).isNull();
            assertThat(event.getAttributes().get(AttributeKey.stringKey("exception.stacktrace"))).isNull();
        });
        assertThat(span.getAttributes().toString()).doesNotContain("private diagnostic");
    }

    @Test
    void rawOutputIsProtobufJson() throws Exception {
        service.unaryResponse = lastChunk("Hello");

        model().generateContent(List.of(Content.newBuilder().setRole("user")
            .addParts(Part.newBuilder().setText("Say hello")).build()));

        SpanData span = exporter.getFinishedSpanItems().get(0);
        var output = JsonParser.parseString(attr(span, "raw.output")).getAsJsonObject();
        assertThat(output.getAsJsonArray("candidates")).hasSize(1);
        assertThat(attr(span, "fi.raw_input")).isNull();
        assertThat(attr(span, "fi.raw_output")).isNull();
        assertThat(span.getAttributes().get(AttributeKey.stringArrayKey("gen_ai.response.finish_reasons")))
            .containsExactly("STOP");
        var input = JsonParser.parseString(attr(span, "raw.input")).getAsJsonArray();
        assertThat(input.get(0).getAsJsonObject().get("role").getAsString()).isEqualTo("user");
        assertThat(attr(span, SemanticConventions.LLM_REQUEST_MODEL)).isEqualTo("gemini-test");
    }
}
