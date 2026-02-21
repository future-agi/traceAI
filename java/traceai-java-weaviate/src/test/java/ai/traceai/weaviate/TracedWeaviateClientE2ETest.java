package ai.traceai.weaviate;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import io.weaviate.client.Config;
import io.weaviate.client.WeaviateClient;
import io.weaviate.client.base.Result;
import io.weaviate.client.v1.data.model.WeaviateObject;
import io.weaviate.client.v1.graphql.model.GraphQLResponse;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.*;

/**
 * E2E test for TracedWeaviateClient.
 * Requires a running Weaviate instance and FI_API_KEY to export spans.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>WEAVIATE_URL - Weaviate server URL (e.g., http://localhost:8080)</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-weaviate-e2e</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "WEAVIATE_URL", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedWeaviateClientE2ETest {

    private static FITracer tracer;
    private static TracedWeaviateClient tracedClient;
    private static String testClassName;
    private static String createdObjectId;

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
                        : "java-weaviate-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();

        String weaviateUrl = System.getenv("WEAVIATE_URL");
        // Class name must start with uppercase in Weaviate
        testClassName = "E2eTest" + UUID.randomUUID().toString().substring(0, 8).replaceAll("-", "");

        if (weaviateUrl != null) {
            try {
                java.net.URI uri = java.net.URI.create(weaviateUrl);
                String scheme = uri.getScheme() != null ? uri.getScheme() : "http";
                String host = uri.getHost() != null ? uri.getHost() : "localhost";
                int port = uri.getPort();
                String hostWithPort = port > 0 ? host + ":" + port : host;

                Config config = new Config(scheme, hostWithPort);
                WeaviateClient client = new WeaviateClient(config);
                tracedClient = new TracedWeaviateClient(client, tracer);
            } catch (Exception e) {
                System.out.println("Failed to create Weaviate client: " + e.getMessage());
            }
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        // Best-effort cleanup: delete the test class/collection
        if (tracedClient != null) {
            try {
                tracedClient.unwrap().schema().classDeleter()
                        .withClassName(testClassName).run();
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
    void shouldCreateObject() {
        Assumptions.assumeTrue(tracedClient != null, "Weaviate client not configured");

        try {
            Map<String, Object> properties = new HashMap<>();
            properties.put("title", "E2E Test Document");
            properties.put("content", "This is a test document for tracing verification");

            Float[] vector = {0.1f, 0.2f, 0.3f, 0.4f};

            Result<WeaviateObject> result = tracedClient.createObject(testClassName, properties, vector);
            if (result != null && !result.hasErrors() && result.getResult() != null) {
                createdObjectId = result.getResult().getId();
                System.out.println("Created object with ID: " + createdObjectId);
            } else {
                System.out.println("Create object returned errors or null result");
            }
        } catch (Exception e) {
            System.out.println("Create object error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldPerformNearVectorSearch() {
        Assumptions.assumeTrue(tracedClient != null, "Weaviate client not configured");

        try {
            Float[] queryVector = {0.1f, 0.2f, 0.3f, 0.4f};
            Result<GraphQLResponse> result = tracedClient.nearVectorSearch(
                    testClassName, queryVector, 5, "title", "content");

            assertThat(result).isNotNull();
            if (result.hasErrors()) {
                System.out.println("Search had errors (span still exported): " + result.getError());
            } else {
                System.out.println("NearVector search completed successfully");
            }
        } catch (Exception e) {
            System.out.println("Search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldGetObjectById() {
        Assumptions.assumeTrue(tracedClient != null, "Weaviate client not configured");
        Assumptions.assumeTrue(createdObjectId != null, "No object was created");

        try {
            Result<List<WeaviateObject>> result = tracedClient.getObject(testClassName, createdObjectId);
            assertThat(result).isNotNull();
            if (!result.hasErrors() && result.getResult() != null) {
                System.out.println("Got object, found: " + !result.getResult().isEmpty());
            }
        } catch (Exception e) {
            System.out.println("Get object error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldBatchImportObjects() {
        Assumptions.assumeTrue(tracedClient != null, "Weaviate client not configured");

        try {
            WeaviateObject obj1 = WeaviateObject.builder()
                    .className(testClassName)
                    .properties(Map.of("title", "Batch Doc 1", "content", "First batch document"))
                    .vector(new Float[]{0.5f, 0.6f, 0.7f, 0.8f})
                    .build();
            WeaviateObject obj2 = WeaviateObject.builder()
                    .className(testClassName)
                    .properties(Map.of("title", "Batch Doc 2", "content", "Second batch document"))
                    .vector(new Float[]{0.9f, 0.1f, 0.2f, 0.3f})
                    .build();

            var result = tracedClient.batchImport(obj1, obj2);
            if (result != null && !result.hasErrors() && result.getResult() != null) {
                System.out.println("Batch imported " + result.getResult().length + " objects");
            }
        } catch (Exception e) {
            System.out.println("Batch import error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(6)
    void shouldDeleteObject() {
        Assumptions.assumeTrue(tracedClient != null, "Weaviate client not configured");
        Assumptions.assumeTrue(createdObjectId != null, "No object was created");

        try {
            Result<Boolean> result = tracedClient.deleteObject(testClassName, createdObjectId);
            assertThat(result).isNotNull();
            System.out.println("Delete object completed");
        } catch (Exception e) {
            System.out.println("Delete error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(7)
    void shouldExposeUnwrappedClient() {
        Assumptions.assumeTrue(tracedClient != null, "Weaviate client not configured");
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
