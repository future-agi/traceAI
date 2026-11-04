package ai.traceai.pgvector;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.pgvector.PGvector;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.postgresql.ds.PGSimpleDataSource;
import org.postgresql.util.PGobject;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/** Runs only against an explicitly supplied, disposable localhost database. */
@EnabledIfSystemProperty(named = "pgvector.test.url", matches = "jdbc:postgresql://(127\\.0\\.0\\.1|localhost):[0-9]+/.*")
class TracedPgVectorStoreDatabaseTest {
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private FITracer tracer;
    private PGSimpleDataSource dataSource;

    @BeforeEach
    void setUp() throws SQLException {
        dataSource = new PGSimpleDataSource();
        dataSource.setURL(System.getProperty("pgvector.test.url"));
        dataSource.setUser("postgres");
        provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        tracer = new FITracer(provider.get("pgvector-db-test"),
            TraceConfig.builder().hideErrorDetails(true).build());
        try (Connection connection = dataSource.getConnection(); var statement = connection.createStatement()) {
            statement.execute("CREATE EXTENSION IF NOT EXISTS vector");
            statement.execute("DROP TABLE IF EXISTS traceai_vectors");
            statement.execute("CREATE TABLE traceai_vectors (id TEXT PRIMARY KEY, embedding vector(3), metadata JSONB)");
            statement.execute("INSERT INTO traceai_vectors VALUES ('one', '[1,2,3]', '{\"category\":\"first\"}'), "
                + "('two', '[4,5,6]', '{\"category\":\"second\"}'), ('null', NULL, NULL)");
        }
    }

    @AfterEach
    void tearDown() throws SQLException {
        try (Connection connection = dataSource.getConnection(); var statement = connection.createStatement()) {
            statement.execute("DROP TABLE IF EXISTS traceai_vectors");
        } finally {
            provider.close();
        }
    }

    @Test
    void unregisteredDataSourceReturnsRealNonemptyResultsOnEveryBorrow() throws SQLException {
        List<Connection> borrowed = new ArrayList<>();
        DataSource tracking = mock(DataSource.class);
        when(tracking.getConnection()).thenAnswer(invocation -> {
            Connection connection = dataSource.getConnection();
            borrowed.add(connection);
            return connection;
        });
        TracedPgVectorStore store = new TracedPgVectorStore(tracking, tracer);
        for (int i = 0; i < 3; i++) {
            List<TracedPgVectorStore.SearchResult> results = store.search("traceai_vectors", new float[]{1, 2, 3}, 5, "l2");
            assertThat(results).extracting(TracedPgVectorStore.SearchResult::getId).containsExactly("one", "two", "null");
            assertThat(results.get(0).getEmbedding()).containsExactly(1, 2, 3);
            assertThat(results.get(0).getDistance()).isZero();
            assertThat(results.get(0).getMetadata()).containsEntry("category", "first");
            assertThat(results.get(1).getEmbedding()).containsExactly(4, 5, 6);
            assertThat(results.get(2).getEmbedding()).isNull();
        }
        assertThat(borrowed).hasSize(4); // Catalog lookup, then three independent searches.
        for (Connection connection : borrowed) {
            assertThat(connection.isClosed()).isTrue();
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(3).allSatisfy(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().get(AttributeKey.longKey("db.vector.results.count"))).isEqualTo(3L);
        });
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void supportsRegisteredAndUnregisteredCallerOwnedConnections(boolean registered) throws SQLException {
        try (Connection connection = dataSource.getConnection()) {
            if (registered) {
                PGvector.registerTypes(connection);
            }
            try (var statement = connection.createStatement(); var rows = statement.executeQuery(
                    "SELECT embedding FROM traceai_vectors WHERE id = 'one'")) {
                assertThat(rows.next()).isTrue();
                assertThat(rows.getObject(1)).isExactlyInstanceOf(registered ? PGvector.class : PGobject.class);
            }
            connection.setAutoCommit(false);
            TracedPgVectorStore store = new TracedPgVectorStore(connection, tracer);
            store.insert("traceai_vectors", "three", new float[]{7, 8, 9}, Map.of("category", "third"));
            // Repeated prepared queries also exercise binary transfer after the driver's prepare threshold.
            for (int i = 0; i < 7; i++) {
                var results = store.searchWithFilter("traceai_vectors", new float[]{7, 8, 9}, 1, "cosine",
                    "metadata->>'category' = 'third'");
                assertThat(results).hasSize(1);
                assertThat(results.get(0).getId()).isEqualTo("three");
                assertThat(results.get(0).getEmbedding()).containsExactly(7, 8, 9);
            }
            assertThat(connection.isClosed()).isFalse();
            assertThat(connection.getAutoCommit()).isFalse();
            connection.rollback();
            assertThat(store.searchWithFilter("traceai_vectors", new float[]{7, 8, 9}, 1, "l2", "id = 'three'")).isEmpty();
            connection.rollback();
        }
    }

    @Test
    void preservesDatabaseFailuresAndRecordsErrorStatus() throws SQLException {
        TracedPgVectorStore store = new TracedPgVectorStore(dataSource, tracer);
        assertThatThrownBy(() -> store.search("traceai_vectors", new float[]{1, 2}, 1, "l2"))
            .isInstanceOf(SQLException.class);
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getStatus().getDescription()).isEmpty();
            assertThat(span.getEvents()).hasSize(1);
        });
    }
}
