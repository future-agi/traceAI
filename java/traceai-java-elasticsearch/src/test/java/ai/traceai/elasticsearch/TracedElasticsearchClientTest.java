package ai.traceai.elasticsearch;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.elasticsearch._types.Result;
import co.elastic.clients.elasticsearch._types.ShardStatistics;
import co.elastic.clients.elasticsearch.core.*;
import co.elastic.clients.elasticsearch.core.bulk.BulkResponseItem;
import co.elastic.clients.elasticsearch.core.search.Hit;
import co.elastic.clients.elasticsearch.core.search.HitsMetadata;
import co.elastic.clients.elasticsearch.core.search.TotalHits;
import co.elastic.clients.elasticsearch.core.search.TotalHitsRelation;
import co.elastic.clients.elasticsearch.indices.CreateIndexResponse;
import co.elastic.clients.elasticsearch.indices.ElasticsearchIndicesClient;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.io.IOException;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class TracedElasticsearchClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    void shouldTraceKnnSearchWithCorrectAttributes() throws Exception {
        // Create a mock client that returns a search response
        ElasticsearchClient mockClient = createMockClientForKnnSearch();
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
    void shouldTraceIndexDocumentWithCorrectAttributes() throws Exception {
        // Create a mock client that returns an index response
        ElasticsearchClient mockClient = createMockClientForIndex();
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
        // Create a mock client that returns a delete response
        ElasticsearchClient mockClient = createMockClientForDelete();
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
        ElasticsearchClient mockClient = createMockClientForKnnSearch();
        TracedElasticsearchClient traced = new TracedElasticsearchClient(mockClient, tracer);

        assertThat(traced.unwrap()).isSameAs(mockClient);
    }

    // Helper methods to create mock clients
    // Note: In a real test, you would use a mocking framework like Mockito
    // These are simplified stubs for demonstration

    private ElasticsearchClient createMockClientForKnnSearch() {
        return new MockElasticsearchClient() {
            @Override
            @SuppressWarnings("unchecked")
            public <TDocument> SearchResponse<TDocument> search(
                Function<SearchRequest.Builder, co.elastic.clients.util.ObjectBuilder<SearchRequest>> fn,
                Class<TDocument> tDocumentClass) throws IOException {

                // Build a minimal search response
                return (SearchResponse<TDocument>) SearchResponse.of(b -> b
                    .took(10)
                    .timedOut(false)
                    .shards(s -> s.total(1).successful(1).skipped(0).failed(0))
                    .hits(h -> h
                        .total(t -> t.value(2).relation(TotalHitsRelation.Eq))
                        .hits(List.of())
                    )
                );
            }
        };
    }

    private ElasticsearchClient createMockClientForIndex() {
        return new MockElasticsearchClient() {
            @Override
            public <TDocument> IndexResponse index(
                Function<IndexRequest.Builder<TDocument>, co.elastic.clients.util.ObjectBuilder<IndexRequest<TDocument>>> fn) throws IOException {
                return IndexResponse.of(b -> b
                    .index("test-index")
                    .id("doc-1")
                    .version(1)
                    .result(Result.Created)
                    .primaryTerm(1)
                    .seqNo(1)
                    .shards(s -> s.total(1).successful(1).failed(0))
                );
            }
        };
    }

    private ElasticsearchClient createMockClientForDelete() {
        return new MockElasticsearchClient() {
            @Override
            public DeleteResponse delete(
                Function<DeleteRequest.Builder, co.elastic.clients.util.ObjectBuilder<DeleteRequest>> fn) throws IOException {
                return DeleteResponse.of(b -> b
                    .index("test-index")
                    .id("doc-1")
                    .version(1)
                    .result(Result.Deleted)
                    .primaryTerm(1)
                    .seqNo(1)
                    .shards(s -> s.total(1).successful(1).failed(0))
                );
            }
        };
    }

    // Abstract mock client base class
    private static abstract class MockElasticsearchClient extends ElasticsearchClient {
        MockElasticsearchClient() {
            super(null, null);
        }

        @Override
        public <TDocument> SearchResponse<TDocument> search(
            Function<SearchRequest.Builder, co.elastic.clients.util.ObjectBuilder<SearchRequest>> fn,
            Class<TDocument> tDocumentClass) throws IOException {
            throw new UnsupportedOperationException("Not implemented");
        }

        @Override
        public <TDocument> IndexResponse index(
            Function<IndexRequest.Builder<TDocument>, co.elastic.clients.util.ObjectBuilder<IndexRequest<TDocument>>> fn) throws IOException {
            throw new UnsupportedOperationException("Not implemented");
        }

        @Override
        public DeleteResponse delete(
            Function<DeleteRequest.Builder, co.elastic.clients.util.ObjectBuilder<DeleteRequest>> fn) throws IOException {
            throw new UnsupportedOperationException("Not implemented");
        }

        @Override
        public BulkResponse bulk(
            Function<BulkRequest.Builder, co.elastic.clients.util.ObjectBuilder<BulkRequest>> fn) throws IOException {
            throw new UnsupportedOperationException("Not implemented");
        }

        @Override
        public ElasticsearchIndicesClient indices() {
            throw new UnsupportedOperationException("Not implemented");
        }
    }
}
