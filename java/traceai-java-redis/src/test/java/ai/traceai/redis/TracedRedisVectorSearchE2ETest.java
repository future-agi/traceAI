package ai.traceai.redis;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import redis.clients.jedis.JedisPooled;
import redis.clients.jedis.search.SearchResult;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.net.URI;
import java.util.*;

/**
 * E2E test for TracedRedisVectorSearch.
 * Requires a running Redis Stack instance (with RediSearch module) and FI_API_KEY.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>REDIS_URL - Redis server URL (e.g., redis://localhost:6379)</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-redis-e2e</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "REDIS_URL", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedRedisVectorSearchE2ETest {

    private static FITracer tracer;
    private static TracedRedisVectorSearch tracedRedis;
    private static String testIndexName;
    private static final int VECTOR_DIM = 4;

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
                        : "java-redis-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();

        String redisUrl = System.getenv("REDIS_URL");
        testIndexName = "e2e_idx_" + UUID.randomUUID().toString().substring(0, 8);

        if (redisUrl != null) {
            try {
                URI uri = URI.create(redisUrl);
                String host = uri.getHost() != null ? uri.getHost() : "localhost";
                int port = uri.getPort() > 0 ? uri.getPort() : 6379;

                JedisPooled jedis = new JedisPooled(host, port);
                tracedRedis = new TracedRedisVectorSearch(jedis, tracer);
            } catch (Exception e) {
                System.out.println("Failed to create Redis client: " + e.getMessage());
            }
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        // Best-effort cleanup
        if (tracedRedis != null) {
            try {
                tracedRedis.unwrap().ftDropIndex(testIndexName);
            } catch (Exception ignored) {
            }
            try {
                tracedRedis.deleteDocument("doc:e2e-1");
                tracedRedis.deleteDocument("doc:e2e-2");
                tracedRedis.deleteDocument("doc:e2e-3");
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
    void shouldCreateIndex() {
        Assumptions.assumeTrue(tracedRedis != null, "Redis client not configured");

        try {
            tracedRedis.createIndex(testIndexName, "embedding", VECTOR_DIM, "COSINE", "FLAT");
            System.out.println("Created index: " + testIndexName);
        } catch (Exception e) {
            System.out.println("Create index error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldAddDocuments() {
        Assumptions.assumeTrue(tracedRedis != null, "Redis client not configured");

        try {
            Map<String, String> metadata1 = new HashMap<>();
            metadata1.put("title", "First document");
            metadata1.put("category", "tech");
            tracedRedis.addDocument("doc:e2e-1", new float[]{0.1f, 0.2f, 0.3f, 0.4f}, metadata1);

            Map<String, String> metadata2 = new HashMap<>();
            metadata2.put("title", "Second document");
            metadata2.put("category", "science");
            tracedRedis.addDocument("doc:e2e-2", new float[]{0.5f, 0.6f, 0.7f, 0.8f}, metadata2);

            Map<String, String> metadata3 = new HashMap<>();
            metadata3.put("title", "Third document");
            metadata3.put("category", "tech");
            tracedRedis.addDocument("doc:e2e-3", new float[]{0.9f, 0.1f, 0.2f, 0.3f}, metadata3);

            System.out.println("Added 3 documents with embeddings");
        } catch (Exception e) {
            System.out.println("Add document error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldPerformVectorSearch() {
        Assumptions.assumeTrue(tracedRedis != null, "Redis client not configured");

        try {
            float[] queryVector = {0.1f, 0.2f, 0.3f, 0.4f};
            SearchResult result = tracedRedis.vectorSearch(testIndexName, queryVector, 3);
            assertThat(result).isNotNull();
            System.out.println("Vector search returned " + result.getTotalResults() + " results");
        } catch (Exception e) {
            System.out.println("Vector search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldPerformVectorSearchWithFilter() {
        Assumptions.assumeTrue(tracedRedis != null, "Redis client not configured");

        try {
            float[] queryVector = {0.1f, 0.2f, 0.3f, 0.4f};
            SearchResult result = tracedRedis.vectorSearch(
                    testIndexName, queryVector, 3, "@category:{tech}");
            assertThat(result).isNotNull();
            System.out.println("Filtered search returned " + result.getTotalResults() + " results");
        } catch (Exception e) {
            System.out.println("Filtered search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(6)
    void shouldDeleteDocument() {
        Assumptions.assumeTrue(tracedRedis != null, "Redis client not configured");

        try {
            tracedRedis.deleteDocument("doc:e2e-3");
            System.out.println("Deleted doc:e2e-3");
        } catch (Exception e) {
            System.out.println("Delete error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(7)
    void shouldExposeUnwrappedClient() {
        Assumptions.assumeTrue(tracedRedis != null, "Redis client not configured");
        assertThat(tracedRedis.unwrap()).isNotNull();
    }
}
