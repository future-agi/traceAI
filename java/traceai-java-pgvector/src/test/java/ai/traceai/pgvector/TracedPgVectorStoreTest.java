package ai.traceai.pgvector;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.MockitoAnnotations;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

/**
 * Unit tests for TracedPgVectorStore.
 */
class TracedPgVectorStoreTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private DataSource mockDataSource;

    @Mock
    private Connection mockConnection;

    @Mock
    private Statement mockStatement;

    @Mock
    private PreparedStatement mockPreparedStatement;

    @Mock
    private ResultSet mockResultSet;

    private FITracer tracer;
    private TracedPgVectorStore store;

    @BeforeEach
    void setUp() throws SQLException {
        MockitoAnnotations.openMocks(this);

        Tracer otelTracer = otelTesting.getOpenTelemetry().getTracer("test-tracer");
        tracer = new FITracer(otelTracer, TraceConfig.builder().build());

        when(mockDataSource.getConnection()).thenReturn(mockConnection);
        when(mockConnection.createStatement()).thenReturn(mockStatement);
        when(mockConnection.prepareStatement(anyString())).thenReturn(mockPreparedStatement);
        when(mockConnection.getCatalog()).thenReturn("test_database");

        store = new TracedPgVectorStore(mockDataSource, tracer);
    }

    @Test
    void testCreateTable() throws SQLException {
        // When
        store.createTable("documents", 1536);

        // Then
        verify(mockStatement, times(2)).execute(anyString());
        verify(mockConnection).close();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Create Table");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("pgvector");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("db.system")))
            .isEqualTo("postgresql");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(1536L);
    }

    @Test
    void testCreateIndex() throws SQLException {
        // When
        store.createIndex("documents", "hnsw", 16);

        // Then
        verify(mockStatement).execute(contains("CREATE INDEX"));
        verify(mockConnection).close();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Create Index");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("pgvector.index_type")))
            .isEqualTo("hnsw");
    }

    @Test
    void testInsert() throws SQLException {
        float[] embedding = new float[1536];
        Map<String, Object> metadata = Map.of("title", "Test Document");

        // When
        store.insert("documents", "doc1", embedding, metadata);

        // Then
        verify(mockPreparedStatement).executeUpdate();
        verify(mockConnection).close();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Insert");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
    }

    @Test
    void testSearch() throws SQLException {
        float[] queryVector = new float[1536];

        when(mockPreparedStatement.executeQuery()).thenReturn(mockResultSet);
        when(mockResultSet.next()).thenReturn(false);

        // When
        List<TracedPgVectorStore.SearchResult> results = store.search("documents", queryVector, 10, "cosine");

        // Then
        assertThat(results).isEmpty();
        verify(mockConnection).close();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Search");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("pgvector.distance_function")))
            .isEqualTo("cosine");
    }

    @Test
    void testSearchWithFilter() throws SQLException {
        float[] queryVector = new float[1536];

        when(mockPreparedStatement.executeQuery()).thenReturn(mockResultSet);
        when(mockResultSet.next()).thenReturn(false);

        // When
        List<TracedPgVectorStore.SearchResult> results = store.searchWithFilter(
            "documents", queryVector, 5, "l2", "metadata->>'category' = 'tech'"
        );

        // Then
        assertThat(results).isEmpty();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.booleanKey("pgvector.has_filter")))
            .isTrue();
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.stringKey("pgvector.distance_function")))
            .isEqualTo("l2");
    }

    @Test
    void testDelete() throws SQLException {
        when(mockPreparedStatement.executeUpdate()).thenReturn(1);

        // When
        boolean deleted = store.delete("documents", "doc1");

        // Then
        assertThat(deleted).isTrue();
        verify(mockConnection).close();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Delete");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey("pgvector.rows_deleted")))
            .isEqualTo(1L);
    }

    @Test
    void testDeleteAll() throws SQLException {
        when(mockStatement.executeUpdate(anyString())).thenReturn(100);

        // When
        int count = store.deleteAll("documents");

        // Then
        assertThat(count).isEqualTo(100);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Delete All");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey("pgvector.rows_deleted")))
            .isEqualTo(100L);
    }

    @Test
    void testCount() throws SQLException {
        when(mockStatement.executeQuery(anyString())).thenReturn(mockResultSet);
        when(mockResultSet.next()).thenReturn(true);
        when(mockResultSet.getLong(1)).thenReturn(42L);

        // When
        long count = store.count("documents");

        // Then
        assertThat(count).isEqualTo(42L);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Count");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey("pgvector.count")))
            .isEqualTo(42L);
    }

    @Test
    void testDistanceFunctionParsing() {
        assertThat(TracedPgVectorStore.DistanceFunction.fromString("l2"))
            .isEqualTo(TracedPgVectorStore.DistanceFunction.L2);
        assertThat(TracedPgVectorStore.DistanceFunction.fromString("cosine"))
            .isEqualTo(TracedPgVectorStore.DistanceFunction.COSINE);
        assertThat(TracedPgVectorStore.DistanceFunction.fromString("inner_product"))
            .isEqualTo(TracedPgVectorStore.DistanceFunction.INNER_PRODUCT);
        assertThat(TracedPgVectorStore.DistanceFunction.fromString("<->"))
            .isEqualTo(TracedPgVectorStore.DistanceFunction.L2);
        assertThat(TracedPgVectorStore.DistanceFunction.fromString("<=>"))
            .isEqualTo(TracedPgVectorStore.DistanceFunction.COSINE);
        assertThat(TracedPgVectorStore.DistanceFunction.fromString("<#>"))
            .isEqualTo(TracedPgVectorStore.DistanceFunction.INNER_PRODUCT);
        assertThat(TracedPgVectorStore.DistanceFunction.fromString(null))
            .isEqualTo(TracedPgVectorStore.DistanceFunction.COSINE);
    }

    @Test
    void testInvalidTableName() {
        assertThatThrownBy(() -> store.createTable("invalid-table-name!", 1536))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("Invalid identifier");
    }

    @Test
    void testSqlExceptionHandling() throws SQLException {
        when(mockStatement.execute(anyString())).thenThrow(new SQLException("Database error"));

        // When/Then
        assertThatThrownBy(() -> store.createTable("documents", 1536))
            .isInstanceOf(SQLException.class)
            .hasMessageContaining("Database error");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getStatus().getStatusCode())
            .isEqualTo(io.opentelemetry.api.trace.StatusCode.ERROR);
    }

    @Test
    void testBatchInsert() throws SQLException {
        List<String> ids = List.of("doc1", "doc2", "doc3");
        List<float[]> embeddings = List.of(
            new float[1536],
            new float[1536],
            new float[1536]
        );
        List<Map<String, Object>> metadatas = List.of(
            Map.of("title", "Doc 1"),
            Map.of("title", "Doc 2"),
            Map.of("title", "Doc 3")
        );

        when(mockPreparedStatement.executeBatch()).thenReturn(new int[]{1, 1, 1});

        // When
        store.batchInsert("documents", ids, embeddings, metadatas);

        // Then
        verify(mockPreparedStatement, times(3)).addBatch();
        verify(mockPreparedStatement).executeBatch();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Batch Insert");
        assertThat(span.getAttributes().get(io.opentelemetry.api.common.AttributeKey.longKey("pgvector.batch_size")))
            .isEqualTo(3L);
    }

    @Test
    void testDropTable() throws SQLException {
        // When
        store.dropTable("documents");

        // Then
        verify(mockStatement).execute(contains("DROP TABLE"));

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("PgVector Drop Table");
    }

    @Test
    void testSearchResult() {
        float[] embedding = new float[]{0.1f, 0.2f, 0.3f};
        Map<String, Object> metadata = Map.of("key", "value");

        TracedPgVectorStore.SearchResult result = new TracedPgVectorStore.SearchResult(
            "id1", embedding, 0.5, metadata
        );

        assertThat(result.getId()).isEqualTo("id1");
        assertThat(result.getEmbedding()).isEqualTo(embedding);
        assertThat(result.getDistance()).isEqualTo(0.5);
        assertThat(result.getMetadata()).isEqualTo(metadata);
    }
}
