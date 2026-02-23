package ai.traceai.qdrant;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import io.qdrant.client.QdrantClient;
import io.qdrant.client.QdrantGrpcClient;
import io.qdrant.client.grpc.Collections.Distance;
import io.qdrant.client.grpc.Points.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.net.URI;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;

import static io.qdrant.client.PointIdFactory.id;
import static io.qdrant.client.VectorsFactory.vectors;
import static io.qdrant.client.ValueFactory.value;

/**
 * E2E test for TracedQdrantClient.
 * Requires a running Qdrant instance and FI_API_KEY to export spans.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>QDRANT_URL - Qdrant server URL (e.g., http://localhost:6334)</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-qdrant-e2e</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "QDRANT_URL", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedQdrantClientE2ETest {

    private static FITracer tracer;
    private static TracedQdrantClient tracedClient;
    private static String testCollection;

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
                        : "java-qdrant-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();

        String qdrantUrl = System.getenv("QDRANT_URL");
        if (qdrantUrl != null) {
            try {
                URI uri = URI.create(qdrantUrl);
                String host = uri.getHost() != null ? uri.getHost() : "localhost";
                int port = uri.getPort() > 0 ? uri.getPort() : 6334;
                boolean useTls = "https".equalsIgnoreCase(uri.getScheme());

                QdrantClient client = new QdrantClient(
                        QdrantGrpcClient.newBuilder(host, port, useTls).build());
                tracedClient = new TracedQdrantClient(client, tracer);
            } catch (Exception e) {
                System.out.println("Failed to create Qdrant client: " + e.getMessage());
            }
        }

        testCollection = "e2e_test_" + UUID.randomUUID().toString().substring(0, 8);
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        // Best-effort cleanup of the test collection
        if (tracedClient != null) {
            try {
                tracedClient.unwrap().deleteCollectionAsync(testCollection).get();
            } catch (Exception ignored) {
                // Collection may not exist
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
    void shouldCreateCollection() {
        Assumptions.assumeTrue(tracedClient != null, "Qdrant client not configured");

        try {
            tracedClient.createCollection(testCollection, 4, Distance.Cosine);
            System.out.println("Created collection: " + testCollection);
        } catch (Exception e) {
            System.out.println("Create collection error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldListCollections() {
        Assumptions.assumeTrue(tracedClient != null, "Qdrant client not configured");

        try {
            List<String> collections = tracedClient.listCollections();
            assertThat(collections).isNotNull();
            System.out.println("Listed " + collections.size() + " collections");
        } catch (Exception e) {
            System.out.println("List collections error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldUpsertPoints() {
        Assumptions.assumeTrue(tracedClient != null, "Qdrant client not configured");

        try {
            List<PointStruct> points = Arrays.asList(
                    PointStruct.newBuilder()
                            .setId(id(1))
                            .setVectors(vectors(Arrays.asList(0.1f, 0.2f, 0.3f, 0.4f)))
                            .putPayload("text", value("first document"))
                            .build(),
                    PointStruct.newBuilder()
                            .setId(id(2))
                            .setVectors(vectors(Arrays.asList(0.5f, 0.6f, 0.7f, 0.8f)))
                            .putPayload("text", value("second document"))
                            .build()
            );

            UpdateResult result = tracedClient.upsert(testCollection, points);
            assertThat(result).isNotNull();
            System.out.println("Upserted 2 points, status: " + result.getStatus().name());
        } catch (Exception e) {
            System.out.println("Upsert error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldSearchVectors() {
        Assumptions.assumeTrue(tracedClient != null, "Qdrant client not configured");

        try {
            List<Float> queryVector = Arrays.asList(0.1f, 0.2f, 0.3f, 0.4f);
            List<ScoredPoint> results = tracedClient.search(testCollection, queryVector, 5);
            assertThat(results).isNotNull();
            System.out.println("Search returned " + results.size() + " results");
        } catch (Exception e) {
            System.out.println("Search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(6)
    void shouldGetPointsByIds() {
        Assumptions.assumeTrue(tracedClient != null, "Qdrant client not configured");

        try {
            List<RetrievedPoint> results = tracedClient.get(
                    testCollection,
                    Arrays.asList(id(1), id(2)),
                    true,
                    false
            );
            assertThat(results).isNotNull();
            System.out.println("Retrieved " + results.size() + " points");
        } catch (Exception e) {
            System.out.println("Get error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(7)
    void shouldExposeUnwrappedClient() {
        Assumptions.assumeTrue(tracedClient != null, "Qdrant client not configured");
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
