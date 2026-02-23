package ai.traceai.examples.elasticsearch;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.elasticsearch.TracedElasticsearchClient;
import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.elasticsearch._types.mapping.DenseVectorProperty;
import co.elastic.clients.elasticsearch._types.mapping.Property;
import co.elastic.clients.elasticsearch._types.mapping.TextProperty;
import co.elastic.clients.elasticsearch._types.query_dsl.Query;
import co.elastic.clients.elasticsearch._types.query_dsl.TermQuery;
import co.elastic.clients.elasticsearch.core.BulkResponse;
import co.elastic.clients.elasticsearch.core.IndexResponse;
import co.elastic.clients.elasticsearch.core.SearchResponse;
import co.elastic.clients.elasticsearch.core.search.Hit;
import co.elastic.clients.elasticsearch.indices.CreateIndexResponse;
import co.elastic.clients.json.jackson.JacksonJsonpMapper;
import co.elastic.clients.transport.ElasticsearchTransport;
import co.elastic.clients.transport.rest_client.RestClientTransport;
import org.apache.http.HttpHost;
import org.apache.http.auth.AuthScope;
import org.apache.http.auth.UsernamePasswordCredentials;
import org.apache.http.impl.client.BasicCredentialsProvider;
import org.elasticsearch.client.RestClient;

import java.io.IOException;
import java.util.*;

/**
 * Example demonstrating TraceAI instrumentation with Elasticsearch vector search.
 *
 * <p>This example shows how to:</p>
 * <ul>
 *   <li>Initialize TraceAI with Elasticsearch</li>
 *   <li>Create an index with dense_vector mapping</li>
 *   <li>Index documents with vector embeddings</li>
 *   <li>Perform k-NN vector search</li>
 *   <li>Perform filtered vector search</li>
 * </ul>
 *
 * <p>To run this example:</p>
 * <pre>
 * export ELASTICSEARCH_URL=http://localhost:9200
 * export ELASTICSEARCH_USERNAME=elastic (optional)
 * export ELASTICSEARCH_PASSWORD=password (optional)
 * export TRACEAI_API_KEY=your-traceai-key
 * mvn exec:java
 * </pre>
 */
public class ElasticsearchExample {

    private static final String INDEX_NAME = "traceai_vectors_example";
    private static final int VECTOR_DIMENSIONS = 384; // Common dimension for small models

    public static void main(String[] args) {
        // ============================================================
        // Step 1: Initialize TraceAI
        // TraceAI provides observability for your AI applications,
        // capturing traces for vector database operations.
        // ============================================================
        TraceAI.init(TraceConfig.builder()
            .baseUrl(System.getenv("TRACEAI_BASE_URL"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("java-elasticsearch-example")
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
        System.out.println("========================================");

        // Get Elasticsearch configuration from environment
        String elasticsearchUrl = System.getenv("ELASTICSEARCH_URL");
        String username = System.getenv("ELASTICSEARCH_USERNAME");
        String password = System.getenv("ELASTICSEARCH_PASSWORD");

        if (elasticsearchUrl == null) {
            elasticsearchUrl = "http://localhost:9200";
        }

        System.out.println("Elasticsearch URL: " + elasticsearchUrl);

        ElasticsearchClient esClient = null;
        RestClient restClient = null;

        try {
            // ============================================================
            // Step 2: Create the Elasticsearch client
            // ============================================================
            restClient = createRestClient(elasticsearchUrl, username, password);
            ElasticsearchTransport transport = new RestClientTransport(
                restClient,
                new JacksonJsonpMapper()
            );
            esClient = new ElasticsearchClient(transport);

            // ============================================================
            // Step 3: Wrap with TracedElasticsearchClient
            // This wrapper automatically captures traces for all operations.
            // ============================================================
            TracedElasticsearchClient traced = new TracedElasticsearchClient(esClient);

            // ============================================================
            // Example 1: Create Index with Vector Mapping
            // Creates an index configured for k-NN vector search.
            // TraceAI captures: index name, mapping configuration.
            // ============================================================
            System.out.println("\n1. Create Index with Vector Mapping");
            System.out.println("------------------------------------");

            // Delete index if it exists (for clean demo)
            try {
                esClient.indices().delete(d -> d.index(INDEX_NAME));
                System.out.println("Deleted existing index: " + INDEX_NAME);
            } catch (Exception e) {
                // Index doesn't exist, which is fine
            }

            // Create index with dense_vector field
            Map<String, Property> mappings = new HashMap<>();
            mappings.put("embedding", Property.of(p -> p
                .denseVector(DenseVectorProperty.of(dv -> dv
                    .dims(VECTOR_DIMENSIONS)
                    .index(true)
                    .similarity("cosine")
                ))
            ));
            mappings.put("title", Property.of(p -> p.text(TextProperty.of(t -> t))));
            mappings.put("content", Property.of(p -> p.text(TextProperty.of(t -> t))));
            mappings.put("category", Property.of(p -> p.keyword(k -> k)));

            CreateIndexResponse createResponse = traced.createIndex(INDEX_NAME, mappings);
            System.out.println("Index created: " + createResponse.acknowledged());

            // ============================================================
            // Example 2: Index Documents with Vectors
            // Indexes documents with vector embeddings.
            // TraceAI captures: index name, document count, dimensions.
            // ============================================================
            System.out.println("\n2. Index Documents with Vectors");
            System.out.println("--------------------------------");

            // Create sample documents with mock embeddings
            List<Map<String, Object>> documents = createSampleDocuments();

            // Index documents using bulk operation
            BulkResponse bulkResponse = traced.bulkIndex(INDEX_NAME, documents);
            System.out.println("Bulk indexed " + documents.size() + " documents");
            System.out.println("Errors: " + bulkResponse.errors());
            System.out.println("Took: " + bulkResponse.took() + "ms");

            // Refresh index to make documents searchable
            esClient.indices().refresh(r -> r.index(INDEX_NAME));

            // ============================================================
            // Example 3: k-NN Vector Search
            // Performs approximate nearest neighbor search.
            // TraceAI captures: query vector, k, results count, scores.
            // ============================================================
            System.out.println("\n3. k-NN Vector Search");
            System.out.println("----------------------");

            // Create a query vector (in practice, this would come from an embedding model)
            float[] queryVector = generateMockEmbedding("machine learning artificial intelligence");

            // Perform k-NN search
            SearchResponse<Map<String, Object>> searchResponse = traced.knnSearch(
                INDEX_NAME,
                queryVector,
                5,      // k - number of results
                100,    // num_candidates - for approximate search
                "embedding"
            );

            System.out.println("Search results:");
            for (Hit<Map<String, Object>> hit : searchResponse.hits().hits()) {
                System.out.printf("  - %.4f: %s%n", hit.score(), hit.source().get("title"));
            }

            // ============================================================
            // Example 4: k-NN Search with Filter
            // Combines vector search with metadata filtering.
            // TraceAI captures: query, filter, results.
            // ============================================================
            System.out.println("\n4. k-NN Search with Filter");
            System.out.println("---------------------------");

            // Create a filter for specific category
            Query filter = Query.of(q -> q
                .term(TermQuery.of(t -> t
                    .field("category")
                    .value("technology")
                ))
            );

            SearchResponse<Map<String, Object>> filteredResponse = traced.knnSearchWithFilter(
                INDEX_NAME,
                queryVector,
                5,
                100,
                "embedding",
                filter
            );

            System.out.println("Filtered search results (category=technology):");
            for (Hit<Map<String, Object>> hit : filteredResponse.hits().hits()) {
                System.out.printf("  - %.4f: %s [%s]%n",
                    hit.score(),
                    hit.source().get("title"),
                    hit.source().get("category"));
            }

            // ============================================================
            // Example 5: Index Single Document
            // Indexes a single document with vector.
            // TraceAI captures: index, document ID, dimensions.
            // ============================================================
            System.out.println("\n5. Index Single Document");
            System.out.println("-------------------------");

            Map<String, Object> newDocument = new HashMap<>();
            newDocument.put("title", "Introduction to Vector Databases");
            newDocument.put("content", "Vector databases store and search high-dimensional vectors efficiently.");
            newDocument.put("category", "technology");
            newDocument.put("embedding", toFloatList(generateMockEmbedding("vector databases search")));

            IndexResponse indexResponse = traced.index(INDEX_NAME, "doc_new", newDocument);
            System.out.println("Indexed document: " + indexResponse.id());
            System.out.println("Result: " + indexResponse.result());

            // ============================================================
            // Example 6: Delete Document
            // Deletes a document from the index.
            // TraceAI captures: index, document ID.
            // ============================================================
            System.out.println("\n6. Delete Document");
            System.out.println("-------------------");

            traced.delete(INDEX_NAME, "doc_new");
            System.out.println("Deleted document: doc_new");

            // ============================================================
            // Cleanup: Delete the test index
            // ============================================================
            System.out.println("\n7. Cleanup");
            System.out.println("----------");
            esClient.indices().delete(d -> d.index(INDEX_NAME));
            System.out.println("Deleted index: " + INDEX_NAME);

        } catch (IOException e) {
            System.err.println("Elasticsearch error: " + e.getMessage());
            e.printStackTrace();
        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            e.printStackTrace();
        } finally {
            // Close REST client
            if (restClient != null) {
                try {
                    restClient.close();
                } catch (IOException e) {
                    // Ignore
                }
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
        System.out.println("  - Elasticsearch Create Index");
        System.out.println("  - Elasticsearch Bulk Index");
        System.out.println("  - Elasticsearch KNN Search (2 calls)");
        System.out.println("  - Elasticsearch Index Document");
        System.out.println("  - Elasticsearch Delete Document");
        System.out.println();
        System.out.println("Each trace includes:");
        System.out.println("  - Index name and operation type");
        System.out.println("  - Vector dimensions");
        System.out.println("  - Search results count and scores");
        System.out.println("  - Performance metrics");

        // Shutdown to flush any pending spans
        TraceAI.shutdown();
    }

    /**
     * Creates a REST client for Elasticsearch.
     */
    private static RestClient createRestClient(String url, String username, String password) {
        HttpHost host = HttpHost.create(url);

        if (username != null && password != null) {
            BasicCredentialsProvider credentialsProvider = new BasicCredentialsProvider();
            credentialsProvider.setCredentials(
                AuthScope.ANY,
                new UsernamePasswordCredentials(username, password)
            );

            return RestClient.builder(host)
                .setHttpClientConfigCallback(httpClientBuilder ->
                    httpClientBuilder.setDefaultCredentialsProvider(credentialsProvider)
                )
                .build();
        }

        return RestClient.builder(host).build();
    }

    /**
     * Creates sample documents with mock embeddings.
     */
    private static List<Map<String, Object>> createSampleDocuments() {
        List<Map<String, Object>> documents = new ArrayList<>();

        // Document 1
        Map<String, Object> doc1 = new HashMap<>();
        doc1.put("_id", "doc1");
        doc1.put("title", "Introduction to Machine Learning");
        doc1.put("content", "Machine learning is a branch of artificial intelligence that enables computers to learn from data.");
        doc1.put("category", "technology");
        doc1.put("embedding", toFloatList(generateMockEmbedding("machine learning AI data")));
        documents.add(doc1);

        // Document 2
        Map<String, Object> doc2 = new HashMap<>();
        doc2.put("_id", "doc2");
        doc2.put("title", "Deep Learning Neural Networks");
        doc2.put("content", "Deep learning uses neural networks with multiple layers to model complex patterns in data.");
        doc2.put("category", "technology");
        doc2.put("embedding", toFloatList(generateMockEmbedding("deep learning neural networks")));
        documents.add(doc2);

        // Document 3
        Map<String, Object> doc3 = new HashMap<>();
        doc3.put("_id", "doc3");
        doc3.put("title", "Natural Language Processing");
        doc3.put("content", "NLP enables computers to understand, interpret, and generate human language.");
        doc3.put("category", "technology");
        doc3.put("embedding", toFloatList(generateMockEmbedding("NLP language processing text")));
        documents.add(doc3);

        // Document 4
        Map<String, Object> doc4 = new HashMap<>();
        doc4.put("_id", "doc4");
        doc4.put("title", "History of Computing");
        doc4.put("content", "The history of computing spans from mechanical calculators to modern quantum computers.");
        doc4.put("category", "history");
        doc4.put("embedding", toFloatList(generateMockEmbedding("computing history quantum")));
        documents.add(doc4);

        // Document 5
        Map<String, Object> doc5 = new HashMap<>();
        doc5.put("_id", "doc5");
        doc5.put("title", "Cloud Computing Fundamentals");
        doc5.put("content", "Cloud computing provides on-demand access to computing resources over the internet.");
        doc5.put("category", "technology");
        doc5.put("embedding", toFloatList(generateMockEmbedding("cloud computing resources")));
        documents.add(doc5);

        return documents;
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

        // Normalize the vector
        norm = (float) Math.sqrt(norm);
        for (int i = 0; i < VECTOR_DIMENSIONS; i++) {
            embedding[i] /= norm;
        }

        return embedding;
    }

    /**
     * Converts float array to List<Float> for Elasticsearch.
     */
    private static List<Float> toFloatList(float[] array) {
        List<Float> list = new ArrayList<>(array.length);
        for (float f : array) {
            list.add(f);
        }
        return list;
    }
}
