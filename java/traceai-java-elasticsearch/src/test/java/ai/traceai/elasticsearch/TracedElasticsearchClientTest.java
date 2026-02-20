package ai.traceai.elasticsearch;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.elasticsearch._types.Result;
import co.elastic.clients.elasticsearch.core.*;
import co.elastic.clients.elasticsearch.core.search.TotalHitsRelation;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedElasticsearchClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private ElasticsearchClient mockClient;

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldTraceKnnSearchWithCorrectAttributes() throws Exception {
        SearchResponse<Map<String, Object>> mockResponse = SearchResponse.of(b -> b
            .took(10)
            .timedOut(false)
            .shards(s -> s.total(1).successful(1).skipped(0).failed(0))
            .hits(h -> h
                .total(t -> t.value(2).relation(TotalHitsRelation.Eq))
                .hits(List.of())
            )
        );

        when(mockClient.search(any(java.util.function.Function.class), any(Class.class)))
            .thenReturn(mockResponse);

        TracedElasticsearchClient traced = new TracedElasticsearchClient(mockClient, tracer);

        float[] queryVector = new float[]{0.1f, 0.2f, 0.3f, 0.4f};
        SearchResponse<Map<String, Object>> response = traced.knnSearch(
            "test-index", queryVector, 10, 100, "embedding"
        );

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Elasticsearch KNN Search");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("elasticsearch");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("db.system")))
            .isEqualTo("elasticsearch");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("elasticsearch.index")))
            .isEqualTo("test-index");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(4L);
        assertThat(span.getAttributes().get(AttributeKey.longKey("elasticsearch.num_candidates")))
            .isEqualTo(100L);
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldTraceIndexDocumentWithCorrectAttributes() throws Exception {
        IndexResponse mockResponse = IndexResponse.of(b -> b
            .index("test-index")
            .id("doc-1")
            .version(1)
            .result(Result.Created)
            .primaryTerm(1)
            .seqNo(1)
            .shards(s -> s.total(1).successful(1).failed(0))
        );

        when(mockClient.index(any(java.util.function.Function.class)))
            .thenReturn(mockResponse);

        TracedElasticsearchClient traced = new TracedElasticsearchClient(mockClient, tracer);

        Map<String, Object> document = new HashMap<>();
        document.put("title", "Test Document");
        document.put("embedding", List.of(0.1f, 0.2f, 0.3f));

        IndexResponse response = traced.index("test-index", "doc-1", document);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Elasticsearch Index Document");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey("elasticsearch.index")))
            .isEqualTo("test-index");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("elasticsearch.document_id")))
            .isEqualTo("doc-1");
    }

    @Test
    void shouldTraceDeleteWithCorrectAttributes() throws Exception {
        DeleteResponse mockResponse = DeleteResponse.of(b -> b
            .index("test-index")
            .id("doc-1")
            .version(1)
            .result(Result.Deleted)
            .primaryTerm(1)
            .seqNo(1)
            .shards(s -> s.total(1).successful(1).failed(0))
        );

        when(mockClient.delete(any(java.util.function.Function.class)))
            .thenReturn(mockResponse);

        TracedElasticsearchClient traced = new TracedElasticsearchClient(mockClient, tracer);

        DeleteResponse response = traced.delete("test-index", "doc-1");

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Elasticsearch Delete Document");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey("elasticsearch.index")))
            .isEqualTo("test-index");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("elasticsearch.document_id")))
            .isEqualTo("doc-1");
    }

    @Test
    void shouldReturnUnwrappedClient() {
        TracedElasticsearchClient traced = new TracedElasticsearchClient(mockClient, tracer);
        assertThat(traced.unwrap()).isSameAs(mockClient);
    }
}
