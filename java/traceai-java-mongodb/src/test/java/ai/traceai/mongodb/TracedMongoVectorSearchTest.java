package ai.traceai.mongodb;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.mongodb.client.AggregateIterable;
import com.mongodb.client.MongoCollection;
import com.mongodb.client.MongoCursor;
import com.mongodb.client.result.DeleteResult;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.bson.Document;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.Iterator;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedMongoVectorSearchTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private MongoCollection<Document> mockCollection;

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @SuppressWarnings("unchecked")
    private AggregateIterable<Document> mockEmptyAggregateIterable() {
        AggregateIterable<Document> mockIterable = mock(AggregateIterable.class);
        MongoCursor<Document> mockCursor = mock(MongoCursor.class);
        when(mockCursor.hasNext()).thenReturn(false);
        when(mockIterable.iterator()).thenReturn(mockCursor);
        // Support for-each loop via Iterable.spliterator() fallback
        doReturn(mockCursor).when(mockIterable).iterator();
        return mockIterable;
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldCreateSpanForVectorSearch() {
        AggregateIterable<Document> mockIterable = mockEmptyAggregateIterable();
        when(mockCollection.aggregate(any(List.class))).thenReturn(mockIterable);

        TracedMongoVectorSearch traced = new TracedMongoVectorSearch(mockCollection, tracer, "test-collection");

        List<Double> queryVector = List.of(0.1, 0.2, 0.3, 0.4);
        List<Document> results = traced.vectorSearch(queryVector, "embedding", "vector_index", 10, 100);

        assertThat(results).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("MongoDB Vector Search");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("mongodb");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("mongodb.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("mongodb.index")))
            .isEqualTo("vector_index");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(AttributeKey.longKey("mongodb.num_candidates")))
            .isEqualTo(100L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(4L);
        assertThat(span.getAttributes().get(AttributeKey.stringKey("mongodb.path")))
            .isEqualTo("embedding");
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldCreateSpanForVectorSearchWithFilter() {
        AggregateIterable<Document> mockIterable = mockEmptyAggregateIterable();
        when(mockCollection.aggregate(any(List.class))).thenReturn(mockIterable);

        TracedMongoVectorSearch traced = new TracedMongoVectorSearch(mockCollection, tracer, "test-collection");

        List<Double> queryVector = List.of(0.1, 0.2, 0.3);
        Document filter = new Document("category", "science");
        List<Document> results = traced.vectorSearch(queryVector, "embedding", "vector_index", 5, 50, filter);

        assertThat(results).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("MongoDB Vector Search");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("mongodb");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("mongodb.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(5L);
        assertThat(span.getAttributes().get(AttributeKey.booleanKey("mongodb.has_filter")))
            .isEqualTo(true);
    }

    @Test
    void shouldCreateSpanForInsertOne() {
        TracedMongoVectorSearch traced = new TracedMongoVectorSearch(mockCollection, tracer, "test-collection");

        Document document = new Document("title", "Test Doc")
            .append("embedding", List.of(0.1, 0.2, 0.3));

        traced.insertOne(document);

        verify(mockCollection).insertOne(document);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("MongoDB Insert");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("mongodb");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("mongodb.collection")))
            .isEqualTo("test-collection");
    }

    @Test
    void shouldCreateSpanForInsertMany() {
        TracedMongoVectorSearch traced = new TracedMongoVectorSearch(mockCollection, tracer, "test-collection");

        List<Document> documents = List.of(
            new Document("title", "Doc 1"),
            new Document("title", "Doc 2"),
            new Document("title", "Doc 3")
        );

        traced.insertMany(documents);

        verify(mockCollection).insertMany(documents);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("MongoDB Insert Many");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("mongodb");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("mongodb.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey("mongodb.insert_count")))
            .isEqualTo(3L);
    }

    @Test
    void shouldCreateSpanForDeleteMany() {
        DeleteResult mockDeleteResult = mock(DeleteResult.class);
        when(mockDeleteResult.getDeletedCount()).thenReturn(5L);
        when(mockCollection.deleteMany(any(Document.class))).thenReturn(mockDeleteResult);

        TracedMongoVectorSearch traced = new TracedMongoVectorSearch(mockCollection, tracer, "test-collection");

        Document filter = new Document("category", "obsolete");
        long deletedCount = traced.deleteMany(filter);

        assertThat(deletedCount).isEqualTo(5L);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("MongoDB Delete");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("mongodb");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("mongodb.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey("mongodb.deleted_count")))
            .isEqualTo(5L);
    }

    @Test
    void shouldReturnUnwrappedCollection() {
        TracedMongoVectorSearch traced = new TracedMongoVectorSearch(mockCollection, tracer, "test-collection");
        assertThat(traced.unwrap()).isSameAs(mockCollection);
    }
}
