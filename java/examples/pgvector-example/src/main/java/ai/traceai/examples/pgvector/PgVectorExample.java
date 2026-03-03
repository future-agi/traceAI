package ai.traceai.examples.pgvector;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.pgvector.TracedPgVectorStore;
import ai.traceai.pgvector.TracedPgVectorStore.SearchResult;
import com.pgvector.PGvector;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.*;

/**
 * Example demonstrating TraceAI instrumentation with PostgreSQL pgvector.
 *
 * <p>This example shows how to:</p>
 * <ul>
 *   <li>Initialize TraceAI with pgvector</li>
 *   <li>Create a table with vector column</li>
 *   <li>Insert vectors with metadata</li>
 *   <li>Perform similarity search</li>
 *   <li>Create vector indexes for performance</li>
 * </ul>
 *
 * <p>Prerequisites:</p>
 * <ul>
 *   <li>PostgreSQL with pgvector extension installed</li>
 *   <li>Database created and accessible</li>
 * </ul>
 *
 * <p>To run this example:</p>
 * <pre>
 * export POSTGRES_URL=jdbc:postgresql://localhost:5432/mydb
 * export POSTGRES_USER=postgres
 * export POSTGRES_PASSWORD=password
 * export TRACEAI_API_KEY=your-traceai-key
 * mvn exec:java
 * </pre>
 */
public class PgVectorExample {

    private static final String TABLE_NAME = "traceai_vectors";
    private static final int VECTOR_DIMENSIONS = 384; // Common dimension for small embedding models

    public static void main(String[] args) {
        // ============================================================
        // Step 1: Initialize TraceAI
        // TraceAI provides observability for your AI applications,
        // capturing traces for vector database operations.
        // ============================================================
        TraceAI.init(TraceConfig.builder()
            .baseUrl(System.getenv("TRACEAI_BASE_URL"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("java-pgvector-example")
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
        System.out.println("========================================");

        // Get PostgreSQL configuration from environment
        String postgresUrl = System.getenv("POSTGRES_URL");
        String postgresUser = System.getenv("POSTGRES_USER");
        String postgresPassword = System.getenv("POSTGRES_PASSWORD");

        if (postgresUrl == null) {
            postgresUrl = "jdbc:postgresql://localhost:5432/postgres";
        }
        if (postgresUser == null) {
            postgresUser = "postgres";
        }
        if (postgresPassword == null) {
            postgresPassword = "";
        }

        System.out.println("PostgreSQL URL: " + postgresUrl);

        HikariDataSource dataSource = null;

        try {
            // ============================================================
            // Step 2: Create the DataSource
            // Using HikariCP for connection pooling (recommended).
            // ============================================================
            HikariConfig config = new HikariConfig();
            config.setJdbcUrl(postgresUrl);
            config.setUsername(postgresUser);
            config.setPassword(postgresPassword);
            config.setMaximumPoolSize(5);

            // Register pgvector type
            config.addDataSourceProperty("preferQueryMode", "extendedForPrepared");

            dataSource = new HikariDataSource(config);

            // Register PGvector type with the connection
            try (Connection conn = dataSource.getConnection()) {
                PGvector.addVectorType(conn);
            }

            // ============================================================
            // Step 3: Wrap with TracedPgVectorStore
            // This wrapper automatically captures traces for all operations.
            // ============================================================
            TracedPgVectorStore store = new TracedPgVectorStore(dataSource);

            // ============================================================
            // Example 1: Create Table with Vector Column
            // Creates a table configured for vector storage and search.
            // TraceAI captures: table name, dimensions.
            // ============================================================
            System.out.println("\n1. Create Table with Vector Column");
            System.out.println("-----------------------------------");

            // Drop table if exists (for clean demo)
            try {
                store.dropTable(TABLE_NAME);
                System.out.println("Dropped existing table: " + TABLE_NAME);
            } catch (SQLException e) {
                // Table doesn't exist, which is fine
            }

            // Create table
            store.createTable(TABLE_NAME, VECTOR_DIMENSIONS);
            System.out.println("Created table: " + TABLE_NAME + " with " + VECTOR_DIMENSIONS + " dimensions");

            // ============================================================
            // Example 2: Create Index for Fast Search
            // Creates an HNSW index for efficient approximate nearest neighbor search.
            // TraceAI captures: index type, parameters.
            // ============================================================
            System.out.println("\n2. Create HNSW Index");
            System.out.println("--------------------");

            store.createIndex(TABLE_NAME, "hnsw", 16, TracedPgVectorStore.DistanceFunction.COSINE);
            System.out.println("Created HNSW index with m=16 for cosine similarity");

            // ============================================================
            // Example 3: Insert Vectors with Metadata
            // Inserts documents with embeddings and metadata.
            // TraceAI captures: table, document ID, dimensions.
            // ============================================================
            System.out.println("\n3. Insert Vectors with Metadata");
            System.out.println("--------------------------------");

            // Insert sample documents
            insertSampleDocuments(store);
            System.out.println("Inserted 5 sample documents");

            // Get count
            long count = store.count(TABLE_NAME);
            System.out.println("Total documents in table: " + count);

            // ============================================================
            // Example 4: Similarity Search
            // Performs cosine similarity search to find nearest neighbors.
            // TraceAI captures: query vector, top_k, distance function, results.
            // ============================================================
            System.out.println("\n4. Similarity Search (Cosine)");
            System.out.println("------------------------------");

            // Create a query vector (in practice, this would come from an embedding model)
            float[] queryVector = generateMockEmbedding("machine learning AI");

            // Search for top 3 similar documents
            List<SearchResult> results = store.search(TABLE_NAME, queryVector, 3, "cosine");

            System.out.println("Search results (top 3):");
            for (SearchResult result : results) {
                System.out.printf("  - Distance: %.4f | ID: %s | Title: %s%n",
                    result.getDistance(),
                    result.getId(),
                    result.getMetadata().get("title"));
            }

            // ============================================================
            // Example 5: Search with Different Distance Functions
            // Demonstrates L2 (Euclidean) distance search.
            // TraceAI captures: distance function used.
            // ============================================================
            System.out.println("\n5. Search with L2 (Euclidean) Distance");
            System.out.println("---------------------------------------");

            List<SearchResult> l2Results = store.search(TABLE_NAME, queryVector, 3, "l2");

            System.out.println("Search results (L2 distance):");
            for (SearchResult result : l2Results) {
                System.out.printf("  - Distance: %.4f | ID: %s | Title: %s%n",
                    result.getDistance(),
                    result.getId(),
                    result.getMetadata().get("title"));
            }

            // ============================================================
            // Example 6: Search with Filter
            // Combines vector search with metadata filtering.
            // TraceAI captures: filter clause, results.
            // ============================================================
            System.out.println("\n6. Search with Metadata Filter");
            System.out.println("-------------------------------");

            // Search only in 'technology' category
            String whereClause = "metadata->>'category' = 'technology'";
            List<SearchResult> filteredResults = store.searchWithFilter(
                TABLE_NAME, queryVector, 5, "cosine", whereClause
            );

            System.out.println("Filtered results (category=technology):");
            for (SearchResult result : filteredResults) {
                System.out.printf("  - Distance: %.4f | ID: %s | Title: %s%n",
                    result.getDistance(),
                    result.getId(),
                    result.getMetadata().get("title"));
            }

            // ============================================================
            // Example 7: Batch Insert
            // Efficiently inserts multiple vectors at once.
            // TraceAI captures: batch size, dimensions.
            // ============================================================
            System.out.println("\n7. Batch Insert");
            System.out.println("----------------");

            List<String> batchIds = new ArrayList<>();
            List<float[]> batchEmbeddings = new ArrayList<>();
            List<Map<String, Object>> batchMetadata = new ArrayList<>();

            for (int i = 0; i < 3; i++) {
                batchIds.add("batch_doc_" + i);
                batchEmbeddings.add(generateMockEmbedding("batch document " + i));
                Map<String, Object> meta = new HashMap<>();
                meta.put("title", "Batch Document " + i);
                meta.put("category", "batch");
                batchMetadata.add(meta);
            }

            store.batchInsert(TABLE_NAME, batchIds, batchEmbeddings, batchMetadata);
            System.out.println("Batch inserted 3 documents");

            count = store.count(TABLE_NAME);
            System.out.println("Total documents after batch: " + count);

            // ============================================================
            // Example 8: Delete Vector
            // Removes a vector by ID.
            // TraceAI captures: table, document ID.
            // ============================================================
            System.out.println("\n8. Delete Vector");
            System.out.println("-----------------");

            boolean deleted = store.delete(TABLE_NAME, "batch_doc_0");
            System.out.println("Deleted batch_doc_0: " + deleted);

            count = store.count(TABLE_NAME);
            System.out.println("Total documents after delete: " + count);

            // ============================================================
            // Example 9: Update Vector (Upsert)
            // Updates an existing vector or inserts if not exists.
            // TraceAI captures: upsert operation.
            // ============================================================
            System.out.println("\n9. Update Vector (Upsert)");
            System.out.println("--------------------------");

            Map<String, Object> updatedMeta = new HashMap<>();
            updatedMeta.put("title", "Updated Machine Learning Guide");
            updatedMeta.put("category", "technology");
            updatedMeta.put("updated", true);

            store.insert(TABLE_NAME, "doc1", generateMockEmbedding("updated ML guide"), updatedMeta);
            System.out.println("Updated doc1 with new embedding and metadata");

            // ============================================================
            // Cleanup
            // ============================================================
            System.out.println("\n10. Cleanup");
            System.out.println("-----------");

            store.dropTable(TABLE_NAME);
            System.out.println("Dropped table: " + TABLE_NAME);

        } catch (SQLException e) {
            System.err.println("Database error: " + e.getMessage());
            e.printStackTrace();
        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            e.printStackTrace();
        } finally {
            // Close DataSource
            if (dataSource != null) {
                dataSource.close();
            }
        }

        // ============================================================
        // Summary
        // ============================================================
        System.out.println("\n========================================");
        System.out.println("All examples completed!");
        System.out.println("Check your TraceAI dashboard for the traces.");
        System.out.println();
        System.out.println("Traces captured include:");
        System.out.println("  - PgVector Create Table");
        System.out.println("  - PgVector Create Index");
        System.out.println("  - PgVector Insert (multiple)");
        System.out.println("  - PgVector Search (3 calls with different options)");
        System.out.println("  - PgVector Batch Insert");
        System.out.println("  - PgVector Delete");
        System.out.println("  - PgVector Count");
        System.out.println("  - PgVector Drop Table");
        System.out.println();
        System.out.println("Each trace includes:");
        System.out.println("  - Table name and operation type");
        System.out.println("  - Vector dimensions");
        System.out.println("  - Distance function (cosine, l2, inner_product)");
        System.out.println("  - Search results count and top distance");
        System.out.println("  - PostgreSQL-specific attributes");

        // Shutdown to flush any pending spans
        TraceAI.shutdown();
    }

    /**
     * Inserts sample documents into the table.
     */
    private static void insertSampleDocuments(TracedPgVectorStore store) throws SQLException {
        // Document 1
        Map<String, Object> meta1 = new HashMap<>();
        meta1.put("title", "Introduction to Machine Learning");
        meta1.put("category", "technology");
        store.insert(TABLE_NAME, "doc1", generateMockEmbedding("machine learning AI data science"), meta1);

        // Document 2
        Map<String, Object> meta2 = new HashMap<>();
        meta2.put("title", "Deep Learning Neural Networks");
        meta2.put("category", "technology");
        store.insert(TABLE_NAME, "doc2", generateMockEmbedding("deep learning neural networks"), meta2);

        // Document 3
        Map<String, Object> meta3 = new HashMap<>();
        meta3.put("title", "Natural Language Processing");
        meta3.put("category", "technology");
        store.insert(TABLE_NAME, "doc3", generateMockEmbedding("NLP text processing language"), meta3);

        // Document 4
        Map<String, Object> meta4 = new HashMap<>();
        meta4.put("title", "History of Computing");
        meta4.put("category", "history");
        store.insert(TABLE_NAME, "doc4", generateMockEmbedding("computing history machines"), meta4);

        // Document 5
        Map<String, Object> meta5 = new HashMap<>();
        meta5.put("title", "Cloud Computing Basics");
        meta5.put("category", "technology");
        store.insert(TABLE_NAME, "doc5", generateMockEmbedding("cloud computing infrastructure"), meta5);
    }

    /**
     * Generates a mock embedding vector for demonstration.
     * In production, you would use an actual embedding model.
     */
    private static float[] generateMockEmbedding(String text) {
        Random random = new Random(text.hashCode()); // Deterministic based on text
        float[] embedding = new float[VECTOR_DIMENSIONS];
        float norm = 0;

        for (int i = 0; i < VECTOR_DIMENSIONS; i++) {
            embedding[i] = random.nextFloat() * 2 - 1; // Random values between -1 and 1
            norm += embedding[i] * embedding[i];
        }

        // Normalize the vector (important for cosine similarity)
        norm = (float) Math.sqrt(norm);
        for (int i = 0; i < VECTOR_DIMENSIONS; i++) {
            embedding[i] /= norm;
        }

        return embedding;
    }
}
