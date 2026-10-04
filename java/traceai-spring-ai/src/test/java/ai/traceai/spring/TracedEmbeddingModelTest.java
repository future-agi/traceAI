package ai.traceai.spring;

import ai.traceai.SemanticConventions;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.metadata.Usage;
import org.springframework.ai.document.Document;
import org.springframework.ai.embedding.BatchingStrategy;
import org.springframework.ai.embedding.Embedding;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.embedding.EmbeddingOptions;
import org.springframework.ai.embedding.EmbeddingRequest;
import org.springframework.ai.embedding.EmbeddingResponse;
import org.springframework.ai.embedding.EmbeddingResponseMetadata;

import java.util.List;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class TracedEmbeddingModelTest {

    private static final AttributeKey<Long> VECTOR_COUNT = AttributeKey.longKey(SemanticConventions.EMBEDDING_VECTOR_COUNT);
    private static final AttributeKey<Long> DIMENSIONS = AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS);
    private static final AttributeKey<Long> PROMPT_TOKENS = AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT);
    private static final AttributeKey<Long> TOTAL_TOKENS = AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL);
    private static final AttributeKey<String> INPUT_VALUE = AttributeKey.stringKey(SemanticConventions.INPUT_VALUE);

    /** Records which overload was invoked and returns fixed values. */
    static class RecordingModel implements EmbeddingModel {
        final AtomicReference<String> invoked = new AtomicReference<>();
        EmbeddingRequest seenRequest;
        EmbeddingOptions seenOptions;
        BatchingStrategy seenStrategy;
        final EmbeddingResponse response;

        RecordingModel() {
            EmbeddingResponseMetadata metadata = new EmbeddingResponseMetadata("embed-model",
                new Usage() {
                    @Override public Integer getPromptTokens() { return 6; }
                    @Override public Integer getCompletionTokens() { return null; }
                    @Override public Integer getTotalTokens() { return 6; }
                    @Override public Object getNativeUsage() { return "native"; }
                });
            this.response = new EmbeddingResponse(List.of(
                new Embedding(new float[]{1f, 2f, 3f}, 0),
                new Embedding(new float[]{4f, 5f, 6f}, 1)), metadata);
        }

        @Override public EmbeddingResponse call(EmbeddingRequest request) { invoked.set("call"); seenRequest = request; return response; }
        @Override public float[] embed(Document document) { invoked.set("embed-document"); return new float[]{1f, 2f, 3f}; }
        @Override public float[] embed(String text) { invoked.set("embed-string"); return new float[]{1f, 2f, 3f}; }
        @Override public List<float[]> embed(List<String> texts) { invoked.set("embed-list"); return List.of(new float[]{1f, 2f, 3f}, new float[]{4f, 5f, 6f}); }
        @Override public List<float[]> embed(List<Document> documents, EmbeddingOptions options, BatchingStrategy batchingStrategy) {
            invoked.set("embed-batch"); seenOptions = options; seenStrategy = batchingStrategy;
            return List.of(new float[]{1f, 2f, 3f});
        }
        @Override public EmbeddingResponse embedForResponse(List<String> texts) { invoked.set("embedForResponse"); return response; }
        @Override public int dimensions() { invoked.set("dimensions"); return 3; }
        @Override public String getEmbeddingContent(Document document) { invoked.set("getEmbeddingContent"); return "doc-text"; }
    }

    private static TracedEmbeddingModel traced(SpanTestSupport support, RecordingModel delegate) {
        return new TracedEmbeddingModel(delegate, support.tracer, "synthetic");
    }

    @Test
    void callDelegatesExactlyAndRecordsVectorsAndUsage() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            RecordingModel delegate = new RecordingModel();
            EmbeddingRequest request = new EmbeddingRequest(List.of("alpha", "beta"), EmbeddingOptions.builder().model("m").build());
            EmbeddingResponse actual = traced(support, delegate).call(request);

            assertThat(actual).isSameAs(delegate.response);
            assertThat(delegate.invoked.get()).isEqualTo("call");
            assertThat(delegate.seenRequest).isSameAs(request);
            SpanData span = support.single();
            assertThat(span.getName()).isEqualTo("Spring AI Embedding");
            assertThat(span.getAttributes().get(VECTOR_COUNT)).isEqualTo(2L);
            assertThat(span.getAttributes().get(DIMENSIONS)).isEqualTo(3L);
            assertThat(span.getAttributes().get(PROMPT_TOKENS)).isEqualTo(6L);
            assertThat(span.getAttributes().get(TOTAL_TOKENS)).isEqualTo(6L);
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_REQUEST_MODEL))).isEqualTo("m");
            assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_RESPONSE_MODEL))).isEqualTo("embed-model");
            assertThat(span.getAttributes().get(INPUT_VALUE)).isEqualTo("alpha\n---\nbeta");
        }
    }

    @Test
    void everyOverloadDelegatesDirectlyWithOriginalArguments() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            RecordingModel delegate = new RecordingModel();
            TracedEmbeddingModel model = traced(support, delegate);
            Document document = new Document("doc-text");
            EmbeddingOptions options = EmbeddingOptions.builder().model("batch-model").build();
            BatchingStrategy strategy = documents -> List.of(documents);

            assertThat(model.embed(document)).containsExactly(1f, 2f, 3f);
            assertThat(delegate.invoked.get()).isEqualTo("embed-document");
            assertThat(model.embed("hello")).containsExactly(1f, 2f, 3f);
            assertThat(delegate.invoked.get()).isEqualTo("embed-string");
            List<float[]> list = model.embed(List.of("a", "b"));
            assertThat(delegate.invoked.get()).isEqualTo("embed-list");
            assertThat(list).hasSize(2);
            List<float[]> batch = model.embed(List.of(document), options, strategy);
            assertThat(delegate.invoked.get()).isEqualTo("embed-batch");
            assertThat(delegate.seenOptions).isSameAs(options);
            assertThat(delegate.seenStrategy).isSameAs(strategy);
            assertThat(batch).hasSize(1);
            assertThat(model.embedForResponse(List.of("a"))).isSameAs(delegate.response);
            assertThat(delegate.invoked.get()).isEqualTo("embedForResponse");
            assertThat(model.dimensions()).isEqualTo(3);
            assertThat(delegate.invoked.get()).isEqualTo("dimensions");
            assertThat(model.getEmbeddingContent(document)).isEqualTo("doc-text");
            assertThat(delegate.invoked.get()).isEqualTo("getEmbeddingContent");
            assertThat(model.unwrap()).isSameAs(delegate);

            // dimensions() and getEmbeddingContent() open no model-operation span
            assertThat(support.finished()).hasSize(5);
            assertThat(support.finished()).allSatisfy(span -> assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK));
        }
    }

    @Test
    void inputPreviewIsBoundedToFiveTexts() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            RecordingModel delegate = new RecordingModel();
            traced(support, delegate).embed(List.of("1", "2", "3", "4", "5", "6", "7"));
            assertThat(support.single().getAttributes().get(INPUT_VALUE)).isEqualTo("1\n---\n2\n---\n3\n---\n4\n---\n5\n... and 2 more");
            assertThat(support.single().getAttributes().get(VECTOR_COUNT)).isEqualTo(2L);
        }
    }

    @Test
    void nullUsageAndNullResponseAreTolerated() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            EmbeddingModel delegate = new RecordingModel() {
                @Override public EmbeddingResponse call(EmbeddingRequest request) { invoked.set("call"); return null; }
            };
            assertThat(new TracedEmbeddingModel(delegate, support.tracer, "synthetic").call(new EmbeddingRequest(List.of("a"), null))).isNull();
            SpanData span = support.single();
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(PROMPT_TOKENS)).isNull();
            assertThat(span.getAttributes().get(TOTAL_TOKENS)).isNull();
        }
    }

    @Test
    void runtimeExceptionKeepsIdentity() {
        try (SpanTestSupport support = new SpanTestSupport()) {
            IllegalStateException failure = new IllegalStateException("embedding failed");
            EmbeddingModel delegate = new RecordingModel() {
                @Override public EmbeddingResponse call(EmbeddingRequest request) { throw failure; }
            };
            assertThatThrownBy(() -> new TracedEmbeddingModel(delegate, support.tracer, "synthetic")
                .call(new EmbeddingRequest(List.of("a"), null))).isSameAs(failure);
            assertThat(support.single().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(support.spansEnded.get()).isEqualTo(1);
        }
    }
}
