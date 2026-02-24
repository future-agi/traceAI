package ai.traceai.examples.azuresearch;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.azure.search.TracedSearchClient;
import com.azure.core.credential.AzureKeyCredential;
import com.azure.search.documents.SearchClient;
import com.azure.search.documents.SearchClientBuilder;
import com.azure.search.documents.SearchDocument;
import com.azure.search.documents.indexes.SearchIndexClient;
import com.azure.search.documents.indexes.SearchIndexClientBuilder;
import com.azure.search.documents.indexes.models.*;
import com.azure.search.documents.models.IndexDocumentsResult;
import com.azure.search.documents.models.SearchResult;
import com.azure.search.documents.util.SearchPagedIterable;

import java.util.*;

/**
 * Example demonstrating TraceAI instrumentation with Azure AI Search.
 *
 * <p>This example shows how to:</p>
 * <ul>
 *   <li>Initialize TraceAI with Azure AI Search</li>
 *   <li>Perform vector search</li>
 *   <li>Perform hybrid search (text + vector)</li>
 *   <li>Upload and manage documents</li>
 * </ul>
 *
 * <p>To run this example:</p>
 * <pre>
 * export AZURE_SEARCH_ENDPOINT=https://your-service.search.windows.net
 * export AZURE_SEARCH_KEY=your-admin-key
 * export AZURE_SEARCH_INDEX=your-index-name
 * export TRACEAI_API_KEY=your-traceai-key
 * mvn exec:java
 * </pre>
 */
public class AzureSearchExample {

    private static final String INDEX_NAME = "traceai-vectors-demo";
    private static final int VECTOR_DIMENSIONS = 384;

    public static void main(String[] args) {
        // ============================================================
        // Step 1: Initialize TraceAI
        // TraceAI provides observability for your AI applications,
        // capturing traces for search and retrieval operations.
        // ============================================================
        TraceAI.init(TraceConfig.builder()
            .baseUrl(System.getenv("TRACEAI_BASE_URL"))
            .apiKey(System.getenv("TRACEAI_API_KEY"))
            .projectName("java-azure-search-example")
            .enableConsoleExporter(true) // Enable console output for demo
            .build());

        System.out.println("TraceAI initialized (version " + TraceAI.getVersion() + ")");
        System.out.println("========================================");

        // Get Azure Search configuration from environment
        String endpoint = System.getenv("AZURE_SEARCH_ENDPOINT");
        String apiKey = System.getenv("AZURE_SEARCH_KEY");
        String indexName = System.getenv("AZURE_SEARCH_INDEX");

        if (indexName == null) {
            indexName = INDEX_NAME;
        }

        // Validate required environment variables
        if (endpoint == null || apiKey == null) {
            System.err.println("Error: Please set AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KEY");
            System.err.println();
            System.err.println("This example demonstrates the TraceAI Azure AI Search instrumentation.");
            printExampleUsage();
            System.exit(1);
        }

        System.out.println("Azure Search Endpoint: " + endpoint);
        System.out.println("Index Name: " + indexName);

        try {
            // ============================================================
            // Step 2: Create Azure Search clients
            // ============================================================
            AzureKeyCredential credential = new AzureKeyCredential(apiKey);

            // Index client for managing indexes
            SearchIndexClient indexClient = new SearchIndexClientBuilder()
                .endpoint(endpoint)
                .credential(credential)
                .buildClient();

            // Search client for search operations
            SearchClient searchClient = new SearchClientBuilder()
                .endpoint(endpoint)
                .credential(credential)
                .indexName(indexName)
                .buildClient();

            // ============================================================
            // Step 3: Create or update the search index
            // ============================================================
            System.out.println("\n1. Create/Update Search Index");
            System.out.println("------------------------------");

            createOrUpdateIndex(indexClient, indexName);
            System.out.println("Index ready: " + indexName);

            // ============================================================
            // Step 4: Wrap with TracedSearchClient
            // This wrapper automatically captures traces for all operations.
            // ============================================================
            TracedSearchClient traced = new TracedSearchClient(searchClient, indexName);

            // ============================================================
            // Example 1: Upload Documents
            // Uploads documents with vector embeddings.
            // TraceAI captures: document count, operation type.
            // ============================================================
            System.out.println("\n2. Upload Documents");
            System.out.println("--------------------");

            List<SearchDocument> documents = createSampleDocuments();
            IndexDocumentsResult uploadResult = traced.uploadDocuments(documents);
            System.out.println("Uploaded " + documents.size() + " documents");
            System.out.println("Succeeded: " + uploadResult.getResults().stream()
                .filter(r -> r.isSucceeded()).count());

            // Wait for indexing to complete
            Thread.sleep(2000);

            // ============================================================
            // Example 2: Vector Search
            // Performs pure vector similarity search.
            // TraceAI captures: query vector, k, results count, scores.
            // ============================================================
            System.out.println("\n3. Vector Search");
            System.out.println("-----------------");

            // Create a query vector (in practice, from an embedding model)
            List<Float> queryVector = generateMockEmbeddingList("machine learning AI");

            SearchPagedIterable vectorResults = traced.searchWithVector(
                null,           // No text query for pure vector search
                queryVector,
                "contentVector", // Vector field name
                5               // Top K results
            );

            System.out.println("Vector search results:");
            for (SearchResult result : vectorResults) {
                SearchDocument doc = result.getDocument(SearchDocument.class);
                System.out.printf("  - Score: %.4f | %s%n",
                    result.getScore(),
                    doc.get("title"));
            }

            // ============================================================
            // Example 3: Hybrid Search (Text + Vector)
            // Combines full-text and vector search for better relevance.
            // TraceAI captures: text query, vector, hybrid mode.
            // ============================================================
            System.out.println("\n4. Hybrid Search (Text + Vector)");
            System.out.println("---------------------------------");

            SearchPagedIterable hybridResults = traced.hybridSearch(
                "machine learning",  // Text query for BM25
                queryVector,         // Vector for similarity
                "contentVector",     // Vector field
                5                    // Top K
            );

            System.out.println("Hybrid search results:");
            for (SearchResult result : hybridResults) {
                SearchDocument doc = result.getDocument(SearchDocument.class);
                System.out.printf("  - Score: %.4f | %s%n",
                    result.getScore(),
                    doc.get("title"));
            }

            // ============================================================
            // Example 4: Text-Only Search
            // Traditional full-text search with BM25 ranking.
            // TraceAI captures: search text, results.
            // ============================================================
            System.out.println("\n5. Text-Only Search");
            System.out.println("--------------------");

            SearchPagedIterable textResults = traced.search("neural networks", 5);

            System.out.println("Text search results:");
            for (SearchResult result : textResults) {
                SearchDocument doc = result.getDocument(SearchDocument.class);
                System.out.printf("  - Score: %.4f | %s%n",
                    result.getScore(),
                    doc.get("title"));
            }

            // ============================================================
            // Example 5: Vector Search with Filter
            // Combines vector search with metadata filtering.
            // TraceAI captures: vector, filter, results.
            // ============================================================
            System.out.println("\n6. Vector Search with Filter");
            System.out.println("-----------------------------");

            SearchPagedIterable filteredResults = traced.searchWithVectorAndFilter(
                null,
                queryVector,
                "contentVector",
                5,
                "category eq 'technology'"  // OData filter
            );

            System.out.println("Filtered vector search results (category=technology):");
            for (SearchResult result : filteredResults) {
                SearchDocument doc = result.getDocument(SearchDocument.class);
                System.out.printf("  - Score: %.4f | %s [%s]%n",
                    result.getScore(),
                    doc.get("title"),
                    doc.get("category"));
            }

            // ============================================================
            // Example 6: Get Document by Key
            // Retrieves a specific document by its key.
            // TraceAI captures: document key, found status.
            // ============================================================
            System.out.println("\n7. Get Document by Key");
            System.out.println("-----------------------");

            SearchDocument doc = traced.getDocument("doc1", SearchDocument.class);
            if (doc != null) {
                System.out.println("Retrieved document: " + doc.get("title"));
            }

            // ============================================================
            // Example 7: Get Document Count
            // Gets total number of documents in the index.
            // TraceAI captures: total count.
            // ============================================================
            System.out.println("\n8. Get Document Count");
            System.out.println("----------------------");

            long documentCount = traced.getDocumentCount();
            System.out.println("Total documents in index: " + documentCount);

            // ============================================================
            // Example 8: Merge or Upload Documents
            // Updates existing documents or creates new ones.
            // TraceAI captures: operation type, document count.
            // ============================================================
            System.out.println("\n9. Update Document (Merge or Upload)");
            System.out.println("-------------------------------------");

            SearchDocument updatedDoc = new SearchDocument();
            updatedDoc.put("id", "doc1");
            updatedDoc.put("title", "Updated: Introduction to Machine Learning");
            updatedDoc.put("content", "Machine learning is a powerful branch of AI (updated).");
            updatedDoc.put("category", "technology");
            updatedDoc.put("contentVector", generateMockEmbeddingList("machine learning AI updated"));

            IndexDocumentsResult mergeResult = traced.mergeOrUploadDocuments(List.of(updatedDoc));
            System.out.println("Updated document: doc1");

            // ============================================================
            // Example 9: Delete Documents
            // Removes documents from the index.
            // TraceAI captures: operation type, document count.
            // ============================================================
            System.out.println("\n10. Delete Document");
            System.out.println("--------------------");

            SearchDocument toDelete = new SearchDocument();
            toDelete.put("id", "doc5");

            IndexDocumentsResult deleteResult = traced.deleteDocuments(List.of(toDelete));
            System.out.println("Deleted document: doc5");

            // ============================================================
            // Cleanup: Delete the test index (optional)
            // ============================================================
            System.out.println("\n11. Cleanup (optional)");
            System.out.println("-----------------------");
            System.out.println("To delete the index, run: indexClient.deleteIndex(\"" + indexName + "\")");

        } catch (Exception e) {
            System.err.println("Error: " + e.getMessage());
            e.printStackTrace();
        }

        // ============================================================
        // Summary
        // ============================================================
        System.out.println("\n========================================");
        System.out.println("All examples completed!");
        System.out.println("Check your TraceAI dashboard for the traces.");
        System.out.println();
        System.out.println("Traces captured include:");
        System.out.println("  - Azure Search Upload Documents");
        System.out.println("  - Azure Search Vector Query");
        System.out.println("  - Azure Search Hybrid Query");
        System.out.println("  - Azure Search Text Query");
        System.out.println("  - Azure Search Filtered Vector Query");
        System.out.println("  - Azure Search Get Document");
        System.out.println("  - Azure Search Get Document Count");
        System.out.println("  - Azure Search Merge or Upload Documents");
        System.out.println("  - Azure Search Delete Documents");
        System.out.println();
        System.out.println("Each trace includes:");
        System.out.println("  - Index name and search mode");
        System.out.println("  - Vector dimensions");
        System.out.println("  - Results count and top score");
        System.out.println("  - Azure Search-specific attributes");

        // Shutdown to flush any pending spans
        TraceAI.shutdown();
    }

    /**
     * Creates or updates the search index with vector configuration.
     */
    private static void createOrUpdateIndex(SearchIndexClient indexClient, String indexName) {
        // Define fields
        List<SearchField> fields = Arrays.asList(
            new SearchField("id", SearchFieldDataType.STRING)
                .setKey(true)
                .setFilterable(true),
            new SearchField("title", SearchFieldDataType.STRING)
                .setSearchable(true)
                .setFilterable(true),
            new SearchField("content", SearchFieldDataType.STRING)
                .setSearchable(true),
            new SearchField("category", SearchFieldDataType.STRING)
                .setFilterable(true)
                .setFacetable(true),
            new SearchField("contentVector", SearchFieldDataType.collection(SearchFieldDataType.SINGLE))
                .setSearchable(true)
                .setVectorSearchDimensions(VECTOR_DIMENSIONS)
                .setVectorSearchProfileName("vector-profile")
        );

        // Define vector search configuration
        VectorSearch vectorSearch = new VectorSearch()
            .setAlgorithms(Arrays.asList(
                new HnswAlgorithmConfiguration("hnsw-config")
                    .setParameters(new HnswParameters()
                        .setM(4)
                        .setEfConstruction(400)
                        .setEfSearch(500)
                        .setMetric(VectorSearchAlgorithmMetric.COSINE))
            ))
            .setProfiles(Arrays.asList(
                new VectorSearchProfile("vector-profile", "hnsw-config")
            ));

        // Create the index
        SearchIndex index = new SearchIndex(indexName)
            .setFields(fields)
            .setVectorSearch(vectorSearch);

        try {
            indexClient.createOrUpdateIndex(index);
        } catch (Exception e) {
            System.out.println("Note: " + e.getMessage());
        }
    }

    /**
     * Creates sample documents with mock embeddings.
     */
    private static List<SearchDocument> createSampleDocuments() {
        List<SearchDocument> documents = new ArrayList<>();

        // Document 1
        SearchDocument doc1 = new SearchDocument();
        doc1.put("id", "doc1");
        doc1.put("title", "Introduction to Machine Learning");
        doc1.put("content", "Machine learning is a branch of artificial intelligence that enables computers to learn from data.");
        doc1.put("category", "technology");
        doc1.put("contentVector", generateMockEmbeddingList("machine learning AI data science"));
        documents.add(doc1);

        // Document 2
        SearchDocument doc2 = new SearchDocument();
        doc2.put("id", "doc2");
        doc2.put("title", "Deep Learning and Neural Networks");
        doc2.put("content", "Deep learning uses neural networks with multiple layers to model complex patterns in data.");
        doc2.put("category", "technology");
        doc2.put("contentVector", generateMockEmbeddingList("deep learning neural networks"));
        documents.add(doc2);

        // Document 3
        SearchDocument doc3 = new SearchDocument();
        doc3.put("id", "doc3");
        doc3.put("title", "Natural Language Processing");
        doc3.put("content", "NLP enables computers to understand, interpret, and generate human language.");
        doc3.put("category", "technology");
        doc3.put("contentVector", generateMockEmbeddingList("NLP language processing text"));
        documents.add(doc3);

        // Document 4
        SearchDocument doc4 = new SearchDocument();
        doc4.put("id", "doc4");
        doc4.put("title", "History of Computing");
        doc4.put("content", "The history of computing spans from mechanical calculators to modern quantum computers.");
        doc4.put("category", "history");
        doc4.put("contentVector", generateMockEmbeddingList("computing history quantum"));
        documents.add(doc4);

        // Document 5
        SearchDocument doc5 = new SearchDocument();
        doc5.put("id", "doc5");
        doc5.put("title", "Cloud Computing Fundamentals");
        doc5.put("content", "Cloud computing provides on-demand access to computing resources over the internet.");
        doc5.put("category", "technology");
        doc5.put("contentVector", generateMockEmbeddingList("cloud computing infrastructure"));
        documents.add(doc5);

        return documents;
    }

    /**
     * Generates a mock embedding vector for demonstration.
     */
    private static List<Float> generateMockEmbeddingList(String text) {
        Random random = new Random(text.hashCode());
        List<Float> embedding = new ArrayList<>(VECTOR_DIMENSIONS);
        float norm = 0;

        float[] temp = new float[VECTOR_DIMENSIONS];
        for (int i = 0; i < VECTOR_DIMENSIONS; i++) {
            temp[i] = random.nextFloat() * 2 - 1;
            norm += temp[i] * temp[i];
        }

        norm = (float) Math.sqrt(norm);
        for (int i = 0; i < VECTOR_DIMENSIONS; i++) {
            embedding.add(temp[i] / norm);
        }

        return embedding;
    }

    /**
     * Prints example usage.
     */
    private static void printExampleUsage() {
        System.out.println("Example usage:");
        System.out.println("  TracedSearchClient traced = new TracedSearchClient(searchClient, indexName);");
        System.out.println("  SearchPagedIterable results = traced.hybridSearch(text, vector, field, k);");
    }
}
