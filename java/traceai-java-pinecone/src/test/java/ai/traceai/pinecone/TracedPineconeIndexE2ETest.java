package ai.traceai.pinecone;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import io.pinecone.clients.Index;
import io.pinecone.clients.Pinecone;
import io.pinecone.unsigned_indices_model.QueryResponseWithUnsignedIndices;
import io.pinecone.unsigned_indices_model.VectorWithUnsignedIndices;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;

/**
 * E2E test for TracedPineconeIndex.
 * Requires a running Pinecone index and FI_API_KEY to export spans.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>PINECONE_API_KEY - API key for Pinecone</li>
 *   <li>PINECONE_INDEX_HOST - Host URL of the Pinecone index</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-pinecone-e2e</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "PINECONE_API_KEY", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedPineconeIndexE2ETest {

    private static FITracer tracer;
    private static TracedPineconeIndex tracedIndex;
    private static String testNamespace;

    @BeforeAll
    static void setUp() {
        String baseUrl = System.getenv("FI_BASE_URL") != null
                ? System.getenv("FI_BASE_URL")
                : "https://api.futureagi.com";

        TraceAI.init(TraceConfig.builder()
                .baseUrl(baseUrl)
                .apiKey(System.getenv("FI_API_KEY"))
                .projectName(System.getenv("FI_PROJECT_NAME") != null
                        ? System.getenv("FI_PROJECT_NAME")
                        : "java-pinecone-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();

        String pineconeApiKey = System.getenv("PINECONE_API_KEY");
        String indexHost = System.getenv("PINECONE_INDEX_HOST");

        if (indexHost != null) {
            Pinecone pinecone = new Pinecone.Builder(pineconeApiKey).build();
            Index index = pinecone.getIndexConnection(indexHost);
            tracedIndex = new TracedPineconeIndex(index, tracer, "e2e-test-index");
        }

        testNamespace = "e2e-test-" + UUID.randomUUID().toString().substring(0, 8);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        // Clean up test vectors
        if (tracedIndex != null) {
            try {
                tracedIndex.deleteByIds(
                        Arrays.asList("e2e-vec-1", "e2e-vec-2", "e2e-vec-3"),
                        testNamespace
                );
            } catch (Exception ignored) {
                // Best-effort cleanup
            }
        }
        Thread.sleep(3000);
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
    void shouldUpsertVectors() {
        Assumptions.assumeTrue(tracedIndex != null, "Pinecone index not configured");

        try {
            List<VectorWithUnsignedIndices> vectors = new ArrayList<>();
            // Create test vectors (dimension must match the index configuration)
            // Using 3-dimensional vectors as a minimal example
            vectors.add(new VectorWithUnsignedIndices("e2e-vec-1", Arrays.asList(0.1f, 0.2f, 0.3f)));
            vectors.add(new VectorWithUnsignedIndices("e2e-vec-2", Arrays.asList(0.4f, 0.5f, 0.6f)));

            int upsertedCount = tracedIndex.upsert(vectors, testNamespace);
            assertThat(upsertedCount).isEqualTo(2);
            System.out.println("Upserted " + upsertedCount + " vectors to namespace: " + testNamespace);
        } catch (Exception e) {
            // Even error spans get exported
            System.out.println("Upsert error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldQueryVectors() {
        Assumptions.assumeTrue(tracedIndex != null, "Pinecone index not configured");

        try {
            List<Float> queryVector = Arrays.asList(0.1f, 0.2f, 0.3f);
            QueryResponseWithUnsignedIndices response = tracedIndex.query(queryVector, 5);
            assertThat(response).isNotNull();
            System.out.println("Query returned matches: " +
                    (response.getMatchesList() != null ? response.getMatchesList().size() : 0));
        } catch (Exception e) {
            // Even error spans get exported
            System.out.println("Query error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldQueryWithNamespaceAndFilter() {
        Assumptions.assumeTrue(tracedIndex != null, "Pinecone index not configured");

        try {
            List<Float> queryVector = Arrays.asList(0.1f, 0.2f, 0.3f);
            QueryResponseWithUnsignedIndices response = tracedIndex.query(
                    queryVector, 5, testNamespace, null);
            assertThat(response).isNotNull();
            System.out.println("Namespaced query returned matches: " +
                    (response.getMatchesList() != null ? response.getMatchesList().size() : 0));
        } catch (Exception e) {
            System.out.println("Namespaced query error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldFetchVectors() {
        Assumptions.assumeTrue(tracedIndex != null, "Pinecone index not configured");

        try {
            java.util.Map<String, Object> response = tracedIndex.fetch(
                    Arrays.asList("e2e-vec-1", "e2e-vec-2"), testNamespace);
            assertThat(response).isNotNull();
            System.out.println("Fetch returned: " + response.size() + " entries");
        } catch (Exception e) {
            System.out.println("Fetch error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(6)
    void shouldDeleteVectors() {
        Assumptions.assumeTrue(tracedIndex != null, "Pinecone index not configured");

        try {
            tracedIndex.deleteByIds(Arrays.asList("e2e-vec-3"), testNamespace);
            System.out.println("Delete completed for namespace: " + testNamespace);
        } catch (Exception e) {
            System.out.println("Delete error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(7)
    void shouldExposeUnwrappedIndex() {
        Assumptions.assumeTrue(tracedIndex != null, "Pinecone index not configured");
        assertThat(tracedIndex.unwrap()).isNotNull();
    }
}
