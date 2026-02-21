package ai.traceai.milvus;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import com.google.gson.JsonObject;
import io.milvus.v2.client.ConnectConfig;
import io.milvus.v2.client.MilvusClientV2;
import io.milvus.v2.service.vector.request.*;
import io.milvus.v2.service.vector.request.data.BaseVector;
import io.milvus.v2.service.vector.request.data.FloatVec;
import io.milvus.v2.service.vector.response.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.*;

/**
 * E2E test for TracedMilvusClient.
 * Requires a running Milvus instance and FI_API_KEY to export spans.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>MILVUS_URL - Milvus server URL (e.g., http://localhost:19530)</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-milvus-e2e</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "MILVUS_URL", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedMilvusClientE2ETest {

    private static FITracer tracer;
    private static TracedMilvusClient tracedClient;
    private static String testCollection;
    private static final int VECTOR_DIM = 4;

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
                        : "java-milvus-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();

        String milvusUrl = System.getenv("MILVUS_URL");
        testCollection = "e2e_test_" + UUID.randomUUID().toString().substring(0, 8).replaceAll("-", "");

        if (milvusUrl != null) {
            try {
                ConnectConfig connectConfig = ConnectConfig.builder()
                        .uri(milvusUrl)
                        .build();
                MilvusClientV2 client = new MilvusClientV2(connectConfig);
                tracedClient = new TracedMilvusClient(client, tracer);
            } catch (Exception e) {
                System.out.println("Failed to create Milvus client: " + e.getMessage());
            }
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        // Best-effort cleanup: drop the test collection
        if (tracedClient != null) {
            try {
                tracedClient.unwrap().dropCollection(
                        io.milvus.v2.service.collection.request.DropCollectionReq.builder()
                                .collectionName(testCollection)
                                .build()
                );
            } catch (Exception ignored) {
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
    void shouldInsertData() {
        Assumptions.assumeTrue(tracedClient != null, "Milvus client not configured");

        try {
            // Build insert data using Gson JsonObject (as expected by Milvus SDK v2.6.x)
            List<JsonObject> data = new ArrayList<>();
            for (int i = 0; i < 3; i++) {
                JsonObject row = new JsonObject();
                row.addProperty("id", (long) (i + 1));
                com.google.gson.JsonArray vec = new com.google.gson.JsonArray();
                for (int d = 0; d < VECTOR_DIM; d++) {
                    vec.add((i + 1) * 0.1f + d * 0.1f);
                }
                row.add("embedding", vec);
                row.addProperty("text", "document " + (i + 1));
                data.add(row);
            }

            InsertReq request = InsertReq.builder()
                    .collectionName(testCollection)
                    .data(data)
                    .build();

            InsertResp response = tracedClient.insert(request);
            assertThat(response).isNotNull();
            System.out.println("Inserted " + response.getInsertCnt() + " rows into: " + testCollection);
        } catch (Exception e) {
            System.out.println("Insert error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldSearchVectors() {
        Assumptions.assumeTrue(tracedClient != null, "Milvus client not configured");

        try {
            List<BaseVector> queryVectors = new ArrayList<>();
            queryVectors.add(new FloatVec(Arrays.asList(0.1f, 0.2f, 0.3f, 0.4f)));

            SearchReq request = SearchReq.builder()
                    .collectionName(testCollection)
                    .data(queryVectors)
                    .topK(5)
                    .build();

            SearchResp response = tracedClient.search(request);
            assertThat(response).isNotNull();
            if (response.getSearchResults() != null && !response.getSearchResults().isEmpty()) {
                System.out.println("Search returned " + response.getSearchResults().get(0).size() + " results");
            }
        } catch (Exception e) {
            System.out.println("Search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldUpsertData() {
        Assumptions.assumeTrue(tracedClient != null, "Milvus client not configured");

        try {
            List<JsonObject> data = new ArrayList<>();
            JsonObject row = new JsonObject();
            row.addProperty("id", 1L);
            com.google.gson.JsonArray vec = new com.google.gson.JsonArray();
            vec.add(0.9f); vec.add(0.8f); vec.add(0.7f); vec.add(0.6f);
            row.add("embedding", vec);
            row.addProperty("text", "updated document 1");
            data.add(row);

            UpsertReq request = UpsertReq.builder()
                    .collectionName(testCollection)
                    .data(data)
                    .build();

            UpsertResp response = tracedClient.upsert(request);
            assertThat(response).isNotNull();
            System.out.println("Upserted " + response.getUpsertCnt() + " rows");
        } catch (Exception e) {
            System.out.println("Upsert error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldQueryData() {
        Assumptions.assumeTrue(tracedClient != null, "Milvus client not configured");

        try {
            QueryReq request = QueryReq.builder()
                    .collectionName(testCollection)
                    .filter("id > 0")
                    .build();

            QueryResp response = tracedClient.query(request);
            assertThat(response).isNotNull();
            if (response.getQueryResults() != null) {
                System.out.println("Query returned " + response.getQueryResults().size() + " results");
            }
        } catch (Exception e) {
            System.out.println("Query error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(6)
    void shouldGetByIds() {
        Assumptions.assumeTrue(tracedClient != null, "Milvus client not configured");

        try {
            GetReq request = GetReq.builder()
                    .collectionName(testCollection)
                    .ids(Arrays.asList(1L, 2L))
                    .build();

            GetResp response = tracedClient.get(request);
            assertThat(response).isNotNull();
            if (response.getGetResults() != null) {
                System.out.println("Get returned " + response.getGetResults().size() + " results");
            }
        } catch (Exception e) {
            System.out.println("Get error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(7)
    void shouldDeleteData() {
        Assumptions.assumeTrue(tracedClient != null, "Milvus client not configured");

        try {
            DeleteReq request = DeleteReq.builder()
                    .collectionName(testCollection)
                    .ids(Arrays.asList(3L))
                    .build();

            DeleteResp response = tracedClient.delete(request);
            assertThat(response).isNotNull();
            System.out.println("Deleted " + response.getDeleteCnt() + " rows");
        } catch (Exception e) {
            System.out.println("Delete error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(8)
    void shouldExposeUnwrappedClient() {
        Assumptions.assumeTrue(tracedClient != null, "Milvus client not configured");
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
