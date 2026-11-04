package ai.traceai.pgvector;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.pgvector.PGvector;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.postgresql.util.PGobject;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

class TracedPgVectorStoreTest {
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private FITracer tracer;
    private Connection connection;
    private PreparedStatement statement;
    private ResultSet rows;
    private TracedPgVectorStore store;

    @BeforeEach
    void setUp() throws SQLException {
        provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(provider.get("pgvector-test"),
            TraceConfig.builder().hideErrorDetails(true).build());
        connection = mock(Connection.class);
        statement = mock(PreparedStatement.class);
        rows = mock(ResultSet.class);
        when(connection.prepareStatement(anyString())).thenReturn(statement);
        when(statement.executeQuery()).thenReturn(rows);
        store = new TracedPgVectorStore(connection, tracer);
    }

    @AfterEach
    void tearDown() {
        provider.close();
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void decodesNonemptyResultsWithOrWithoutTypeRegistration(boolean registered) throws SQLException {
        PGobject embedding = registered ? new PGvector("[1,2,3]") : new PGobject();
        embedding.setType("vector");
        embedding.setValue("[1,2,3]");
        when(rows.next()).thenReturn(true, false);
        when(rows.getObject("embedding")).thenReturn(embedding);
        when(rows.getString("embedding")).thenReturn(embedding.getValue());
        when(rows.getString("id")).thenReturn("doc1");
        when(rows.getString("metadata")).thenReturn("{\"title\":\"private-content\"}");
        when(rows.getDouble("distance")).thenReturn(0.125);

        List<TracedPgVectorStore.SearchResult> results = store.search("documents", new float[]{1, 2, 3}, 5, "l2");

        assertThat(results).hasSize(1);
        assertThat(results.get(0).getId()).isEqualTo("doc1");
        assertThat(results.get(0).getEmbedding()).containsExactly(1, 2, 3);
        assertThat(results.get(0).getDistance()).isEqualTo(0.125);
        assertThat(results.get(0).getMetadata()).containsEntry("title", "private-content");
        SpanData span = onlySpan();
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.DB_VECTOR_RESULTS_COUNT))).isEqualTo(1L);
        assertThat(span.getAttributes().toString()).doesNotContain("private-content", "[1,2,3]");
        verify(connection, never()).close();
        verify(rows).close();
        verify(statement).close();
    }

    @Test
    void preservesNullEmbeddingAndEmptyResults() throws SQLException {
        when(rows.next()).thenReturn(true, false, false);
        List<TracedPgVectorStore.SearchResult> results = store.search("documents", new float[]{1, 2, 3}, 5, "l2");
        assertThat(results).hasSize(1);
        assertThat(results.get(0).getEmbedding()).isNull();
        assertThat(store.search("documents", new float[]{1, 2, 3}, 5, "l2")).isEmpty();
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
    }

    @Test
    void marksDecodingFailuresAsErrorsAndClosesBorrowedResources() throws SQLException {
        DataSource dataSource = mock(DataSource.class);
        when(dataSource.getConnection()).thenReturn(connection);
        TracedPgVectorStore borrowed = new TracedPgVectorStore(dataSource, tracer);
        clearInvocations(connection); // The constructor borrows one connection to read its catalog.
        when(rows.next()).thenReturn(true);
        PGobject malformed = new PGobject();
        malformed.setType("vector");
        malformed.setValue("[private-content]");
        when(rows.getObject("embedding")).thenReturn(malformed);
        when(rows.getString("embedding")).thenReturn(malformed.getValue());

        assertThatThrownBy(() -> borrowed.search("documents", new float[]{1}, 1, "cosine"))
            .isInstanceOf(RuntimeException.class);

        assertPrivateError(onlySpan());
        verify(connection).close();
        verify(statement).close();
        verify(rows).close();
    }

    @Test
    void preservesSqlExceptions() throws SQLException {
        SQLException failure = new SQLException("private-content");
        when(statement.executeQuery()).thenThrow(failure);
        assertThatThrownBy(() -> store.search("documents", new float[]{1}, 1, "cosine"))
            .isSameAs(failure);
        assertPrivateError(onlySpan());
        verify(statement).close();
        verify(connection, never()).close();
    }

    @ParameterizedTest
    @ValueSource(strings = {"create", "index", "insert", "search", "delete", "deleteAll", "batch", "count", "drop"})
    void marksRuntimeFailuresForEveryOperation(String operation) throws SQLException {
        when(connection.createStatement()).thenReturn(mock(java.sql.Statement.class));
        assertThatThrownBy(() -> {
            switch (operation) {
                case "create": store.createTable("private-content!", 3); break;
                case "index": store.createIndex("private-content!", "hnsw", 16); break;
                case "insert": store.insert("private-content!", "doc1", new float[]{1}, null); break;
                case "search": store.search("private-content!", new float[]{1}, 1, "l2"); break;
                case "delete": store.delete("private-content!", "doc1"); break;
                case "deleteAll": store.deleteAll("private-content!"); break;
                case "batch": store.batchInsert("private-content!", List.of("doc1"), List.of(new float[]{1}), null); break;
                case "count": store.count("private-content!"); break;
                case "drop": store.dropTable("private-content!"); break;
                default: throw new AssertionError(operation);
            }
        }).isInstanceOf(IllegalArgumentException.class);
        assertPrivateError(onlySpan());
        verify(connection, never()).close();
    }

    private SpanData onlySpan() {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        return exporter.getFinishedSpanItems().get(0);
    }

    private void assertPrivateError(SpanData span) {
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(span.getStatus().getDescription()).isEmpty();
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.ERROR_TYPE))).isNotNull();
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.ERROR_MESSAGE))).isNull();
        assertThat(span.getEvents()).hasSize(1);
        assertThat(span.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.message"))).isNull();
        assertThat(span.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("exception.stacktrace"))).isNull();
    }
}
