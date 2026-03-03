package ai.traceai.pgvector;

import ai.traceai.*;
import com.pgvector.PGvector;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for PostgreSQL pgvector operations.
 * Wraps JDBC connections to provide automatic tracing of all vector database operations.
 *
 * <p>Usage:</p>
 * <pre>
 * DataSource dataSource = ...; // Your PostgreSQL DataSource
 * TracedPgVectorStore store = new TracedPgVectorStore(dataSource);
 *
 * // Create table with vector column
 * store.createTable("documents", 1536);
 *
 * // Insert vector
 * float[] embedding = new float[1536];
 * store.insert("documents", "doc1", embedding, Map.of("title", "My Document"));
 *
 * // Search for similar vectors
 * List&lt;SearchResult&gt; results = store.search("documents", queryVector, 10, "cosine");
 * </pre>
 */
public class TracedPgVectorStore {

    private final DataSource dataSource;
    private final Connection connection;
    private final FITracer tracer;
    private final String databaseName;

    /**
     * Distance function operators for pgvector.
     */
    public enum DistanceFunction {
        /** Euclidean distance (L2) - operator: &lt;-&gt; */
        L2("<->", "l2"),
        /** Cosine distance - operator: &lt;=&gt; */
        COSINE("<=>", "cosine"),
        /** Inner product (negative) - operator: &lt;#&gt; */
        INNER_PRODUCT("<#>", "inner_product");

        private final String operator;
        private final String name;

        DistanceFunction(String operator, String name) {
            this.operator = operator;
            this.name = name;
        }

        public String getOperator() {
            return operator;
        }

        public String getName() {
            return name;
        }

        /**
         * Parses a distance function from string.
         *
         * @param value the string value (l2, cosine, inner_product, or the operators)
         * @return the distance function
         */
        public static DistanceFunction fromString(String value) {
            if (value == null) {
                return COSINE; // default
            }
            String lower = value.toLowerCase().trim();
            switch (lower) {
                case "l2":
                case "euclidean":
                case "<->":
                    return L2;
                case "cosine":
                case "<=>":
                    return COSINE;
                case "inner_product":
                case "ip":
                case "<#>":
                    return INNER_PRODUCT;
                default:
                    return COSINE;
            }
        }
    }

    /**
     * Index types supported by pgvector.
     */
    public enum IndexType {
        /** IVFFlat index - good for recall with large datasets */
        IVFFLAT("ivfflat"),
        /** HNSW index - better performance for most use cases */
        HNSW("hnsw");

        private final String value;

        IndexType(String value) {
            this.value = value;
        }

        public String getValue() {
            return value;
        }

        public static IndexType fromString(String value) {
            if (value == null) {
                return HNSW;
            }
            if (value.equalsIgnoreCase("ivfflat")) {
                return IVFFLAT;
            }
            return HNSW;
        }
    }

    /**
     * Represents a search result from pgvector.
     */
    public static class SearchResult {
        private final String id;
        private final float[] embedding;
        private final double distance;
        private final Map<String, Object> metadata;

        public SearchResult(String id, float[] embedding, double distance, Map<String, Object> metadata) {
            this.id = id;
            this.embedding = embedding;
            this.distance = distance;
            this.metadata = metadata;
        }

        public String getId() {
            return id;
        }

        public float[] getEmbedding() {
            return embedding;
        }

        public double getDistance() {
            return distance;
        }

        public Map<String, Object> getMetadata() {
            return metadata;
        }
    }

    /**
     * Creates a new traced pgvector store with a DataSource and tracer.
     *
     * @param dataSource the PostgreSQL DataSource
     * @param tracer     the FITracer for instrumentation
     */
    public TracedPgVectorStore(DataSource dataSource, FITracer tracer) {
        this.dataSource = dataSource;
        this.connection = null;
        this.tracer = tracer;
        this.databaseName = extractDatabaseName(dataSource);
    }

    /**
     * Creates a new traced pgvector store with a DataSource using the global TraceAI tracer.
     *
     * @param dataSource the PostgreSQL DataSource
     */
    public TracedPgVectorStore(DataSource dataSource) {
        this(dataSource, TraceAI.getTracer());
    }

    /**
     * Creates a new traced pgvector store with a Connection and tracer.
     *
     * @param connection the PostgreSQL Connection
     * @param tracer     the FITracer for instrumentation
     */
    public TracedPgVectorStore(Connection connection, FITracer tracer) {
        this.dataSource = null;
        this.connection = connection;
        this.tracer = tracer;
        this.databaseName = extractDatabaseName(connection);
    }

    /**
     * Creates a new traced pgvector store with a Connection using the global TraceAI tracer.
     *
     * @param connection the PostgreSQL Connection
     */
    public TracedPgVectorStore(Connection connection) {
        this(connection, TraceAI.getTracer());
    }

    /**
     * Creates a table with a vector column for storing embeddings.
     *
     * @param tableName  the name of the table to create
     * @param dimensions the dimensionality of the vectors
     * @throws SQLException if a database error occurs
     */
    public void createTable(String tableName, int dimensions) throws SQLException {
        Span span = tracer.startSpan("PgVector Create Table", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) dimensions);
            span.setAttribute("db.operation", "CREATE TABLE");

            try (Connection conn = getConnection();
                 Statement stmt = conn.createStatement()) {

                // Enable pgvector extension if not already enabled
                stmt.execute("CREATE EXTENSION IF NOT EXISTS vector");

                // Create table with vector column
                String sql = String.format(
                    "CREATE TABLE IF NOT EXISTS %s (" +
                    "id TEXT PRIMARY KEY, " +
                    "embedding vector(%d), " +
                    "metadata JSONB" +
                    ")",
                    sanitizeIdentifier(tableName),
                    dimensions
                );
                stmt.execute(sql);

                span.setStatus(StatusCode.OK);
            }

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Creates an index on the vector column for efficient similarity search.
     *
     * @param tableName the name of the table
     * @param indexType the type of index (ivfflat or hnsw)
     * @param lists     the number of lists (for IVFFlat) or m parameter (for HNSW)
     * @throws SQLException if a database error occurs
     */
    public void createIndex(String tableName, String indexType, int lists) throws SQLException {
        createIndex(tableName, indexType, lists, DistanceFunction.COSINE);
    }

    /**
     * Creates an index on the vector column for efficient similarity search.
     *
     * @param tableName        the name of the table
     * @param indexType        the type of index (ivfflat or hnsw)
     * @param lists            the number of lists (for IVFFlat) or m parameter (for HNSW)
     * @param distanceFunction the distance function for the index
     * @throws SQLException if a database error occurs
     */
    public void createIndex(String tableName, String indexType, int lists, DistanceFunction distanceFunction)
            throws SQLException {
        Span span = tracer.startSpan("PgVector Create Index", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute("pgvector.index_type", indexType);
            span.setAttribute("pgvector.index_lists", (long) lists);
            span.setAttribute("pgvector.distance_function", distanceFunction.getName());
            span.setAttribute("db.operation", "CREATE INDEX");

            IndexType type = IndexType.fromString(indexType);

            try (Connection conn = getConnection();
                 Statement stmt = conn.createStatement()) {

                String indexName = sanitizeIdentifier(tableName + "_embedding_idx");
                String safeTableName = sanitizeIdentifier(tableName);

                String sql;
                if (type == IndexType.IVFFLAT) {
                    // IVFFlat index
                    String opsClass = getOpsClass(distanceFunction, type);
                    sql = String.format(
                        "CREATE INDEX IF NOT EXISTS %s ON %s USING ivfflat (embedding %s) WITH (lists = %d)",
                        indexName, safeTableName, opsClass, lists
                    );
                } else {
                    // HNSW index
                    String opsClass = getOpsClass(distanceFunction, type);
                    sql = String.format(
                        "CREATE INDEX IF NOT EXISTS %s ON %s USING hnsw (embedding %s) WITH (m = %d)",
                        indexName, safeTableName, opsClass, lists
                    );
                }

                stmt.execute(sql);
                span.setStatus(StatusCode.OK);
            }

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Inserts a vector with metadata into the table.
     *
     * @param tableName the name of the table
     * @param id        the unique identifier for the vector
     * @param embedding the vector embedding
     * @param metadata  optional metadata (can be null)
     * @throws SQLException if a database error occurs
     */
    public void insert(String tableName, String id, float[] embedding, Map<String, Object> metadata)
            throws SQLException {
        Span span = tracer.startSpan("PgVector Insert", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute("pgvector.vector_id", id);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embedding.length);
            span.setAttribute("db.operation", "INSERT");

            if (metadata != null) {
                span.setAttribute("pgvector.has_metadata", true);
            }

            try (Connection conn = getConnection()) {
                String sql = String.format(
                    "INSERT INTO %s (id, embedding, metadata) VALUES (?, ?, ?::jsonb) " +
                    "ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata",
                    sanitizeIdentifier(tableName)
                );

                try (PreparedStatement pstmt = conn.prepareStatement(sql)) {
                    pstmt.setString(1, id);
                    pstmt.setObject(2, new PGvector(embedding));
                    pstmt.setString(3, metadata != null ? tracer.toJson(metadata) : "{}");
                    pstmt.executeUpdate();
                }

                span.setStatus(StatusCode.OK);
            }

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Performs a similarity search on the table.
     *
     * @param tableName        the name of the table
     * @param queryVector      the query vector
     * @param topK             the number of results to return
     * @param distanceFunction the distance function (l2, cosine, inner_product)
     * @return list of search results ordered by similarity
     * @throws SQLException if a database error occurs
     */
    public List<SearchResult> search(String tableName, float[] queryVector, int topK, String distanceFunction)
            throws SQLException {
        return searchWithFilter(tableName, queryVector, topK, distanceFunction, null);
    }

    /**
     * Performs a similarity search with a metadata filter.
     *
     * @param tableName        the name of the table
     * @param queryVector      the query vector
     * @param topK             the number of results to return
     * @param distanceFunction the distance function (l2, cosine, inner_product)
     * @param whereClause      optional WHERE clause for filtering (without the WHERE keyword)
     * @return list of search results ordered by similarity
     * @throws SQLException if a database error occurs
     */
    public List<SearchResult> searchWithFilter(String tableName, float[] queryVector, int topK,
                                                String distanceFunction, String whereClause) throws SQLException {
        Span span = tracer.startSpan("PgVector Search", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute(SemanticConventions.RETRIEVER_TOP_K, (long) topK);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) queryVector.length);
            span.setAttribute("db.operation", "SELECT");

            DistanceFunction df = DistanceFunction.fromString(distanceFunction);
            span.setAttribute("pgvector.distance_function", df.getName());

            if (whereClause != null && !whereClause.isEmpty()) {
                span.setAttribute("pgvector.has_filter", true);
            }

            List<SearchResult> results = new ArrayList<>();

            try (Connection conn = getConnection()) {
                String safeTableName = sanitizeIdentifier(tableName);
                String operator = df.getOperator();

                StringBuilder sqlBuilder = new StringBuilder();
                sqlBuilder.append("SELECT id, embedding, metadata, embedding ")
                          .append(operator)
                          .append(" ? AS distance FROM ")
                          .append(safeTableName);

                if (whereClause != null && !whereClause.isEmpty()) {
                    sqlBuilder.append(" WHERE ").append(whereClause);
                }

                sqlBuilder.append(" ORDER BY embedding ")
                          .append(operator)
                          .append(" ? LIMIT ?");

                String sql = sqlBuilder.toString();

                try (PreparedStatement pstmt = conn.prepareStatement(sql)) {
                    PGvector pgVector = new PGvector(queryVector);
                    pstmt.setObject(1, pgVector);
                    pstmt.setObject(2, pgVector);
                    pstmt.setInt(3, topK);

                    try (ResultSet rs = pstmt.executeQuery()) {
                        while (rs.next()) {
                            String id = rs.getString("id");
                            PGvector embeddingVector = (PGvector) rs.getObject("embedding");
                            float[] embedding = embeddingVector != null ? embeddingVector.toArray() : null;
                            double distance = rs.getDouble("distance");
                            String metadataJson = rs.getString("metadata");

                            Map<String, Object> metadata = parseMetadata(metadataJson);

                            results.add(new SearchResult(id, embedding, distance, metadata));
                        }
                    }
                }
            }

            span.setAttribute("pgvector.results_count", (long) results.size());

            if (!results.isEmpty()) {
                span.setAttribute("pgvector.top_distance", results.get(0).getDistance());
            }

            span.setStatus(StatusCode.OK);
            return results;

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Deletes a vector by its ID.
     *
     * @param tableName the name of the table
     * @param id        the ID of the vector to delete
     * @return true if a row was deleted, false otherwise
     * @throws SQLException if a database error occurs
     */
    public boolean delete(String tableName, String id) throws SQLException {
        Span span = tracer.startSpan("PgVector Delete", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute("pgvector.vector_id", id);
            span.setAttribute("db.operation", "DELETE");

            try (Connection conn = getConnection()) {
                String sql = String.format(
                    "DELETE FROM %s WHERE id = ?",
                    sanitizeIdentifier(tableName)
                );

                try (PreparedStatement pstmt = conn.prepareStatement(sql)) {
                    pstmt.setString(1, id);
                    int rowsAffected = pstmt.executeUpdate();

                    span.setAttribute("pgvector.rows_deleted", (long) rowsAffected);
                    span.setStatus(StatusCode.OK);

                    return rowsAffected > 0;
                }
            }

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Deletes all vectors from the table.
     *
     * @param tableName the name of the table
     * @return the number of rows deleted
     * @throws SQLException if a database error occurs
     */
    public int deleteAll(String tableName) throws SQLException {
        Span span = tracer.startSpan("PgVector Delete All", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute("db.operation", "DELETE");

            try (Connection conn = getConnection();
                 Statement stmt = conn.createStatement()) {

                String sql = String.format(
                    "DELETE FROM %s",
                    sanitizeIdentifier(tableName)
                );

                int rowsAffected = stmt.executeUpdate(sql);

                span.setAttribute("pgvector.rows_deleted", (long) rowsAffected);
                span.setStatus(StatusCode.OK);

                return rowsAffected;
            }

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Batch inserts multiple vectors.
     *
     * @param tableName  the name of the table
     * @param ids        the IDs for each vector
     * @param embeddings the vector embeddings
     * @param metadatas  optional metadata for each vector (can be null or contain null entries)
     * @throws SQLException if a database error occurs
     */
    public void batchInsert(String tableName, List<String> ids, List<float[]> embeddings,
                            List<Map<String, Object>> metadatas) throws SQLException {
        if (ids.size() != embeddings.size()) {
            throw new IllegalArgumentException("ids and embeddings must have the same size");
        }

        Span span = tracer.startSpan("PgVector Batch Insert", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute("pgvector.batch_size", (long) ids.size());
            span.setAttribute("db.operation", "INSERT");

            if (!embeddings.isEmpty()) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embeddings.get(0).length);
            }

            try (Connection conn = getConnection()) {
                String sql = String.format(
                    "INSERT INTO %s (id, embedding, metadata) VALUES (?, ?, ?::jsonb) " +
                    "ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata",
                    sanitizeIdentifier(tableName)
                );

                try (PreparedStatement pstmt = conn.prepareStatement(sql)) {
                    for (int i = 0; i < ids.size(); i++) {
                        pstmt.setString(1, ids.get(i));
                        pstmt.setObject(2, new PGvector(embeddings.get(i)));
                        Map<String, Object> meta = (metadatas != null && i < metadatas.size())
                            ? metadatas.get(i) : null;
                        pstmt.setString(3, meta != null ? tracer.toJson(meta) : "{}");
                        pstmt.addBatch();
                    }

                    int[] results = pstmt.executeBatch();
                    span.setAttribute("pgvector.inserted_count", (long) results.length);
                }

                span.setStatus(StatusCode.OK);
            }

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Gets the count of vectors in the table.
     *
     * @param tableName the name of the table
     * @return the number of vectors
     * @throws SQLException if a database error occurs
     */
    public long count(String tableName) throws SQLException {
        Span span = tracer.startSpan("PgVector Count", FISpanKind.RETRIEVER);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute("db.operation", "SELECT COUNT");

            try (Connection conn = getConnection();
                 Statement stmt = conn.createStatement()) {

                String sql = String.format(
                    "SELECT COUNT(*) FROM %s",
                    sanitizeIdentifier(tableName)
                );

                try (ResultSet rs = stmt.executeQuery(sql)) {
                    if (rs.next()) {
                        long count = rs.getLong(1);
                        span.setAttribute("pgvector.count", count);
                        span.setStatus(StatusCode.OK);
                        return count;
                    }
                }
            }

            span.setStatus(StatusCode.OK);
            return 0;

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Drops the table.
     *
     * @param tableName the name of the table to drop
     * @throws SQLException if a database error occurs
     */
    public void dropTable(String tableName) throws SQLException {
        Span span = tracer.startSpan("PgVector Drop Table", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            setCommonAttributes(span);
            span.setAttribute("pgvector.table_name", tableName);
            span.setAttribute("db.operation", "DROP TABLE");

            try (Connection conn = getConnection();
                 Statement stmt = conn.createStatement()) {

                String sql = String.format(
                    "DROP TABLE IF EXISTS %s",
                    sanitizeIdentifier(tableName)
                );
                stmt.execute(sql);

                span.setStatus(StatusCode.OK);
            }

        } catch (SQLException e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    // --- Private helper methods ---

    private Connection getConnection() throws SQLException {
        if (dataSource != null) {
            return dataSource.getConnection();
        }
        if (connection != null) {
            // When using a direct connection, we don't close it
            return new NonClosingConnectionWrapper(connection);
        }
        throw new SQLException("No DataSource or Connection configured");
    }

    private void setCommonAttributes(Span span) {
        span.setAttribute(SemanticConventions.LLM_SYSTEM, "pgvector");
        span.setAttribute("db.system", "postgresql");
        if (databaseName != null) {
            span.setAttribute("db.name", databaseName);
        }
    }

    private String extractDatabaseName(DataSource dataSource) {
        if (dataSource == null) {
            return null;
        }
        try (Connection conn = dataSource.getConnection()) {
            return extractDatabaseName(conn);
        } catch (SQLException e) {
            return null;
        }
    }

    private String extractDatabaseName(Connection connection) {
        if (connection == null) {
            return null;
        }
        try {
            return connection.getCatalog();
        } catch (SQLException e) {
            return null;
        }
    }

    private String sanitizeIdentifier(String identifier) {
        // Basic SQL injection prevention - only allow alphanumeric and underscore
        if (identifier == null || !identifier.matches("^[a-zA-Z_][a-zA-Z0-9_]*$")) {
            throw new IllegalArgumentException("Invalid identifier: " + identifier);
        }
        return identifier;
    }

    private String getOpsClass(DistanceFunction df, IndexType indexType) {
        switch (df) {
            case L2:
                return indexType == IndexType.IVFFLAT ? "vector_l2_ops" : "vector_l2_ops";
            case INNER_PRODUCT:
                return indexType == IndexType.IVFFLAT ? "vector_ip_ops" : "vector_ip_ops";
            case COSINE:
            default:
                return indexType == IndexType.IVFFLAT ? "vector_cosine_ops" : "vector_cosine_ops";
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> parseMetadata(String json) {
        if (json == null || json.isEmpty() || json.equals("null")) {
            return new HashMap<>();
        }
        try {
            com.google.gson.Gson gson = new com.google.gson.Gson();
            return gson.fromJson(json, Map.class);
        } catch (Exception e) {
            return new HashMap<>();
        }
    }

    /**
     * A connection wrapper that prevents closing the underlying connection.
     * Used when a direct Connection is provided to the store.
     */
    private static class NonClosingConnectionWrapper implements Connection {
        private final Connection delegate;

        NonClosingConnectionWrapper(Connection delegate) {
            this.delegate = delegate;
        }

        @Override
        public void close() {
            // Do not close the underlying connection
        }

        // Delegate all other methods to the wrapped connection
        @Override
        public Statement createStatement() throws SQLException {
            return delegate.createStatement();
        }

        @Override
        public PreparedStatement prepareStatement(String sql) throws SQLException {
            return delegate.prepareStatement(sql);
        }

        @Override
        public java.sql.CallableStatement prepareCall(String sql) throws SQLException {
            return delegate.prepareCall(sql);
        }

        @Override
        public String nativeSQL(String sql) throws SQLException {
            return delegate.nativeSQL(sql);
        }

        @Override
        public void setAutoCommit(boolean autoCommit) throws SQLException {
            delegate.setAutoCommit(autoCommit);
        }

        @Override
        public boolean getAutoCommit() throws SQLException {
            return delegate.getAutoCommit();
        }

        @Override
        public void commit() throws SQLException {
            delegate.commit();
        }

        @Override
        public void rollback() throws SQLException {
            delegate.rollback();
        }

        @Override
        public boolean isClosed() throws SQLException {
            return delegate.isClosed();
        }

        @Override
        public java.sql.DatabaseMetaData getMetaData() throws SQLException {
            return delegate.getMetaData();
        }

        @Override
        public void setReadOnly(boolean readOnly) throws SQLException {
            delegate.setReadOnly(readOnly);
        }

        @Override
        public boolean isReadOnly() throws SQLException {
            return delegate.isReadOnly();
        }

        @Override
        public void setCatalog(String catalog) throws SQLException {
            delegate.setCatalog(catalog);
        }

        @Override
        public String getCatalog() throws SQLException {
            return delegate.getCatalog();
        }

        @Override
        public void setTransactionIsolation(int level) throws SQLException {
            delegate.setTransactionIsolation(level);
        }

        @Override
        public int getTransactionIsolation() throws SQLException {
            return delegate.getTransactionIsolation();
        }

        @Override
        public java.sql.SQLWarning getWarnings() throws SQLException {
            return delegate.getWarnings();
        }

        @Override
        public void clearWarnings() throws SQLException {
            delegate.clearWarnings();
        }

        @Override
        public Statement createStatement(int resultSetType, int resultSetConcurrency) throws SQLException {
            return delegate.createStatement(resultSetType, resultSetConcurrency);
        }

        @Override
        public PreparedStatement prepareStatement(String sql, int resultSetType, int resultSetConcurrency)
                throws SQLException {
            return delegate.prepareStatement(sql, resultSetType, resultSetConcurrency);
        }

        @Override
        public java.sql.CallableStatement prepareCall(String sql, int resultSetType, int resultSetConcurrency)
                throws SQLException {
            return delegate.prepareCall(sql, resultSetType, resultSetConcurrency);
        }

        @Override
        public Map<String, Class<?>> getTypeMap() throws SQLException {
            return delegate.getTypeMap();
        }

        @Override
        public void setTypeMap(Map<String, Class<?>> map) throws SQLException {
            delegate.setTypeMap(map);
        }

        @Override
        public void setHoldability(int holdability) throws SQLException {
            delegate.setHoldability(holdability);
        }

        @Override
        public int getHoldability() throws SQLException {
            return delegate.getHoldability();
        }

        @Override
        public java.sql.Savepoint setSavepoint() throws SQLException {
            return delegate.setSavepoint();
        }

        @Override
        public java.sql.Savepoint setSavepoint(String name) throws SQLException {
            return delegate.setSavepoint(name);
        }

        @Override
        public void rollback(java.sql.Savepoint savepoint) throws SQLException {
            delegate.rollback(savepoint);
        }

        @Override
        public void releaseSavepoint(java.sql.Savepoint savepoint) throws SQLException {
            delegate.releaseSavepoint(savepoint);
        }

        @Override
        public Statement createStatement(int resultSetType, int resultSetConcurrency, int resultSetHoldability)
                throws SQLException {
            return delegate.createStatement(resultSetType, resultSetConcurrency, resultSetHoldability);
        }

        @Override
        public PreparedStatement prepareStatement(String sql, int resultSetType, int resultSetConcurrency,
                int resultSetHoldability) throws SQLException {
            return delegate.prepareStatement(sql, resultSetType, resultSetConcurrency, resultSetHoldability);
        }

        @Override
        public java.sql.CallableStatement prepareCall(String sql, int resultSetType, int resultSetConcurrency,
                int resultSetHoldability) throws SQLException {
            return delegate.prepareCall(sql, resultSetType, resultSetConcurrency, resultSetHoldability);
        }

        @Override
        public PreparedStatement prepareStatement(String sql, int autoGeneratedKeys) throws SQLException {
            return delegate.prepareStatement(sql, autoGeneratedKeys);
        }

        @Override
        public PreparedStatement prepareStatement(String sql, int[] columnIndexes) throws SQLException {
            return delegate.prepareStatement(sql, columnIndexes);
        }

        @Override
        public PreparedStatement prepareStatement(String sql, String[] columnNames) throws SQLException {
            return delegate.prepareStatement(sql, columnNames);
        }

        @Override
        public java.sql.Clob createClob() throws SQLException {
            return delegate.createClob();
        }

        @Override
        public java.sql.Blob createBlob() throws SQLException {
            return delegate.createBlob();
        }

        @Override
        public java.sql.NClob createNClob() throws SQLException {
            return delegate.createNClob();
        }

        @Override
        public java.sql.SQLXML createSQLXML() throws SQLException {
            return delegate.createSQLXML();
        }

        @Override
        public boolean isValid(int timeout) throws SQLException {
            return delegate.isValid(timeout);
        }

        @Override
        public void setClientInfo(String name, String value) throws java.sql.SQLClientInfoException {
            delegate.setClientInfo(name, value);
        }

        @Override
        public void setClientInfo(java.util.Properties properties) throws java.sql.SQLClientInfoException {
            delegate.setClientInfo(properties);
        }

        @Override
        public String getClientInfo(String name) throws SQLException {
            return delegate.getClientInfo(name);
        }

        @Override
        public java.util.Properties getClientInfo() throws SQLException {
            return delegate.getClientInfo();
        }

        @Override
        public java.sql.Array createArrayOf(String typeName, Object[] elements) throws SQLException {
            return delegate.createArrayOf(typeName, elements);
        }

        @Override
        public java.sql.Struct createStruct(String typeName, Object[] attributes) throws SQLException {
            return delegate.createStruct(typeName, attributes);
        }

        @Override
        public void setSchema(String schema) throws SQLException {
            delegate.setSchema(schema);
        }

        @Override
        public String getSchema() throws SQLException {
            return delegate.getSchema();
        }

        @Override
        public void abort(java.util.concurrent.Executor executor) throws SQLException {
            delegate.abort(executor);
        }

        @Override
        public void setNetworkTimeout(java.util.concurrent.Executor executor, int milliseconds) throws SQLException {
            delegate.setNetworkTimeout(executor, milliseconds);
        }

        @Override
        public int getNetworkTimeout() throws SQLException {
            return delegate.getNetworkTimeout();
        }

        @Override
        public <T> T unwrap(Class<T> iface) throws SQLException {
            return delegate.unwrap(iface);
        }

        @Override
        public boolean isWrapperFor(Class<?> iface) throws SQLException {
            return delegate.isWrapperFor(iface);
        }
    }
}
