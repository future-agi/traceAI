package ai.traceai.pinecone;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.pinecone.clients.Index;
import io.pinecone.unsigned_indices_model.QueryResponseWithUnsignedIndices;
import io.pinecone.unsigned_indices_model.VectorWithUnsignedIndices;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.Arrays;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedPineconeIndexTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private Index mockIndex;

    @Mock
    private QueryResponseWithUnsignedIndices mockQueryResponse;

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    void shouldCreateSpanForQuery() throws Exception {
        List<Float> queryVector = Arrays.asList(0.1f, 0.2f, 0.3f, 0.4f);

        when(mockQueryResponse.getMatchesList()).thenReturn(List.of());
        when(mockIndex.query(
            anyInt(), anyList(), isNull(), isNull(), isNull(), isNull(), isNull(), anyBoolean(), anyBoolean()
        )).thenReturn(mockQueryResponse);

        TracedPineconeIndex traced = new TracedPineconeIndex(mockIndex, tracer, "test-index");

        QueryResponseWithUnsignedIndices response = traced.query(queryVector, 10);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Pinecone Query");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("pinecone");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("pinecone.index")))
            .isEqualTo("test-index");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(4L);
    }

    @Test
    void shouldCreateSpanForUpsert() {
        @SuppressWarnings("unchecked")
        VectorWithUnsignedIndices mockVector = org.mockito.Mockito.mock(VectorWithUnsignedIndices.class);
        when(mockVector.getValuesList()).thenReturn(Arrays.asList(0.1f, 0.2f, 0.3f));

        List<VectorWithUnsignedIndices> vectors = List.of(mockVector);

        when(mockIndex.upsert(anyList(), any())).thenReturn(null);

        TracedPineconeIndex traced = new TracedPineconeIndex(mockIndex, tracer, "test-index");

        int upsertedCount = traced.upsert(vectors, "test-namespace");

        assertThat(upsertedCount).isEqualTo(1);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Pinecone Upsert");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("pinecone");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("pinecone.index")))
            .isEqualTo("test-index");
        assertThat(span.getAttributes().get(AttributeKey.longKey("pinecone.vectors_count")))
            .isEqualTo(1L);
        assertThat(span.getAttributes().get(AttributeKey.stringKey("pinecone.namespace")))
            .isEqualTo("test-namespace");
    }

    @Test
    void shouldCreateSpanForDeleteByIds() {
        List<String> ids = Arrays.asList("id-1", "id-2");

        when(mockIndex.deleteByIds(anyList(), any())).thenReturn(null);

        TracedPineconeIndex traced = new TracedPineconeIndex(mockIndex, tracer, "test-index");

        traced.deleteByIds(ids, "test-namespace");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Pinecone Delete");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("pinecone");
        assertThat(span.getAttributes().get(AttributeKey.longKey("pinecone.delete_count")))
            .isEqualTo(2L);
    }

    @Test
    void shouldReturnUnwrapped() {
        TracedPineconeIndex traced = new TracedPineconeIndex(mockIndex, tracer, "test-index");
        assertThat(traced.unwrap()).isSameAs(mockIndex);
    }
}
