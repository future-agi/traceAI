package ai.traceai.qdrant;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.qdrant.client.QdrantClient;
import io.qdrant.client.grpc.Common.PointId;
import io.qdrant.client.grpc.Points.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import com.google.common.util.concurrent.Futures;

import java.util.Arrays;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedQdrantClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private QdrantClient mockClient;

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    void shouldCreateSpanForSearch() throws Exception {
        List<ScoredPoint> mockResults = List.of(
            ScoredPoint.newBuilder()
                .setId(PointId.newBuilder().setNum(1).build())
                .setScore(0.95f)
                .build()
        );

        SearchPoints searchRequest = SearchPoints.newBuilder()
            .setCollectionName("test-collection")
            .addAllVector(Arrays.asList(0.1f, 0.2f, 0.3f))
            .setLimit(10)
            .build();

        when(mockClient.searchAsync(any(SearchPoints.class)))
            .thenReturn(Futures.immediateFuture(mockResults));

        TracedQdrantClient traced = new TracedQdrantClient(mockClient, tracer);

        List<ScoredPoint> results = traced.search(searchRequest);

        assertThat(results).hasSize(1);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Qdrant Search");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("qdrant");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("qdrant.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(AttributeKey.longKey("qdrant.results_count")))
            .isEqualTo(1L);
    }

    @Test
    void shouldCreateSpanForUpsert() throws Exception {
        UpdateResult mockResult = UpdateResult.newBuilder()
            .setStatus(UpdateStatus.Completed)
            .build();

        List<PointStruct> points = List.of(
            PointStruct.newBuilder()
                .setId(PointId.newBuilder().setNum(1).build())
                .setVectors(Vectors.newBuilder()
                    .setVector(Vector.newBuilder()
                        .addAllData(Arrays.asList(0.1f, 0.2f, 0.3f))
                        .build())
                    .build())
                .build()
        );

        when(mockClient.upsertAsync(anyString(), anyList()))
            .thenReturn(Futures.immediateFuture(mockResult));

        TracedQdrantClient traced = new TracedQdrantClient(mockClient, tracer);

        UpdateResult result = traced.upsert("test-collection", points);

        assertThat(result).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Qdrant Upsert");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("qdrant");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("qdrant.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey("qdrant.points_count")))
            .isEqualTo(1L);
        assertThat(span.getAttributes().get(AttributeKey.stringKey("qdrant.status")))
            .isEqualTo("Completed");
    }

    @Test
    void shouldCreateSpanForListCollections() throws Exception {
        List<String> mockCollections = Arrays.asList("collection-a", "collection-b");

        when(mockClient.listCollectionsAsync())
            .thenReturn(Futures.immediateFuture(mockCollections));

        TracedQdrantClient traced = new TracedQdrantClient(mockClient, tracer);

        List<String> collections = traced.listCollections();

        assertThat(collections).hasSize(2);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Qdrant List Collections");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("qdrant");
        assertThat(span.getAttributes().get(AttributeKey.longKey("qdrant.collections_count")))
            .isEqualTo(2L);
    }

    @Test
    void shouldReturnUnwrapped() {
        TracedQdrantClient traced = new TracedQdrantClient(mockClient, tracer);
        assertThat(traced.unwrap()).isSameAs(mockClient);
    }
}
