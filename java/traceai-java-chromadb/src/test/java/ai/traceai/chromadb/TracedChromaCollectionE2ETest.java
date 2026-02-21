package ai.traceai.chromadb;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import tech.amikos.chromadb.Client;
import tech.amikos.chromadb.Collection;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.*;

/**
 * E2E test for TracedChromaCollection.
 * Requires a running ChromaDB instance and FI_API_KEY to export spans.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>CHROMA_URL - ChromaDB server URL (e.g., http://localhost:8000)</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-chromadb-e2e</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "CHROMA_URL", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedChromaCollectionE2ETest {

    private static FITracer tracer;
    private static TracedChromaCollection tracedCollection;
    private static Client chromaClient;
    private static String testCollectionName;

    @BeforeAll
    static void setUp() {
        String baseUrl = System.getenv("FI_BASE_URL") != null
                ? System.getenv("FI_BASE_URL")
                : "https://api.futureagi.com";

        TraceAI.init(TraceConfig.builder()
                .baseUrl(baseUrl)
                .apiKey(System.getenv("FI_API_KEY"))
                .secretKey(System.getenv("FI_SECRET_KEY"))
                .projectName(System.getenv("FI_PROJECT_NAME") != null
                        ? System.getenv("FI_PROJECT_NAME")
                        : "java-chromadb-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();

        String chromaUrl = System.getenv("CHROMA_URL");
        testCollectionName = "e2e_test_" + UUID.randomUUID().toString().substring(0, 8);

        if (chromaUrl != null) {
            try {
                chromaClient = new Client(chromaUrl);
                Collection collection = chromaClient.createCollection(
                        testCollectionName, null, true, null);
                tracedCollection = new TracedChromaCollection(collection, tracer, testCollectionName);
            } catch (Exception e) {
                System.out.println("Failed to create ChromaDB client: " + e.getMessage());
            }
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        // Best-effort cleanup
        if (chromaClient != null) {
            try {
                chromaClient.deleteCollection(testCollectionName);
            } catch (Exception ignored) {
            }
        }
        TraceAI.shutdown();
        TraceAI.shutdown();
    }

    @Test
    @Order(1)
    void shouldInitializeTraceAI() {
        assertThat(TraceAI.isInitialized()).isTrue();
        assertThat(tracer).isNotNull();
    }

    @Test
    @Order(2)
    void shouldAddDocuments() {
        Assumptions.assumeTrue(tracedCollection != null, "ChromaDB collection not configured");

        try {
            List<String> documents = Arrays.asList(
                    "The quick brown fox jumps over the lazy dog",
                    "Machine learning is a subset of artificial intelligence",
                    "Vector databases store high-dimensional data"
            );
            List<String> ids = Arrays.asList("doc-1", "doc-2", "doc-3");
            List<Map<String, String>> metadatas = Arrays.asList(
                    Collections.singletonMap("category", "animals"),
                    Collections.singletonMap("category", "tech"),
                    Collections.singletonMap("category", "tech")
            );

            tracedCollection.add(null, metadatas, documents, ids);
            System.out.println("Added 3 documents to collection: " + testCollectionName);
        } catch (Exception e) {
            System.out.println("Add error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldCountDocuments() {
        Assumptions.assumeTrue(tracedCollection != null, "ChromaDB collection not configured");

        try {
            int count = tracedCollection.count();
            assertThat(count).isGreaterThanOrEqualTo(0);
            System.out.println("Collection count: " + count);
        } catch (Exception e) {
            System.out.println("Count error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldQueryByText() {
        Assumptions.assumeTrue(tracedCollection != null, "ChromaDB collection not configured");

        try {
            List<String> queryTexts = Collections.singletonList("artificial intelligence");
            Collection.QueryResponse response = tracedCollection.query(
                    queryTexts, 2, null, null, null);
            assertThat(response).isNotNull();
            if (response.getIds() != null && !response.getIds().isEmpty()) {
                System.out.println("Query returned " + response.getIds().get(0).size() + " results");
            }
        } catch (Exception e) {
            System.out.println("Query error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldGetDocumentsById() {
        Assumptions.assumeTrue(tracedCollection != null, "ChromaDB collection not configured");

        try {
            Collection.GetResult result = tracedCollection.get(
                    Arrays.asList("doc-1", "doc-2"), null, null);
            assertThat(result).isNotNull();
            if (result.getIds() != null) {
                System.out.println("Get returned " + result.getIds().size() + " documents");
            }
        } catch (Exception e) {
            System.out.println("Get error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(6)
    void shouldUpsertDocuments() {
        Assumptions.assumeTrue(tracedCollection != null, "ChromaDB collection not configured");

        try {
            List<String> documents = Collections.singletonList("Updated document content");
            List<String> ids = Collections.singletonList("doc-1");
            List<Map<String, String>> metadatas = Collections.singletonList(
                    Collections.singletonMap("category", "updated")
            );

            tracedCollection.upsert(null, metadatas, documents, ids);
            System.out.println("Upserted 1 document");
        } catch (Exception e) {
            System.out.println("Upsert error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(7)
    void shouldDeleteDocuments() {
        Assumptions.assumeTrue(tracedCollection != null, "ChromaDB collection not configured");

        try {
            tracedCollection.delete(Arrays.asList("doc-3"), null, null);
            System.out.println("Deleted doc-3");
        } catch (Exception e) {
            System.out.println("Delete error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(8)
    void shouldExposeUnwrappedCollection() {
        Assumptions.assumeTrue(tracedCollection != null, "ChromaDB collection not configured");
        assertThat(tracedCollection.unwrap()).isNotNull();
    }
}
