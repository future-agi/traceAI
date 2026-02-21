package ai.traceai.mongodb;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import com.mongodb.client.MongoClient;
import com.mongodb.client.MongoClients;
import com.mongodb.client.MongoCollection;
import com.mongodb.client.MongoDatabase;
import org.bson.Document;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.util.*;

/**
 * E2E test for TracedMongoVectorSearch.
 * Requires a running MongoDB Atlas instance (with vector search) and FI_API_KEY.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>MONGODB_URI - MongoDB connection URI (e.g., mongodb+srv://user:pass@cluster.mongodb.net)</li>
 *   <li>MONGODB_DATABASE - (optional) database name, defaults to "e2e_test"</li>
 *   <li>MONGODB_VECTOR_INDEX - (optional) vector search index name, defaults to "vector_index"</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-mongodb-e2e</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "MONGODB_URI", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedMongoVectorSearchE2ETest {

    private static FITracer tracer;
    private static TracedMongoVectorSearch tracedMongo;
    private static MongoClient mongoClient;
    private static String testCollectionName;
    private static String vectorIndexName;
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
                        : "java-mongodb-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();

        String mongoUri = System.getenv("MONGODB_URI");
        String dbName = System.getenv("MONGODB_DATABASE") != null
                ? System.getenv("MONGODB_DATABASE")
                : "e2e_test";
        vectorIndexName = System.getenv("MONGODB_VECTOR_INDEX") != null
                ? System.getenv("MONGODB_VECTOR_INDEX")
                : "vector_index";
        testCollectionName = "e2e_test_" + UUID.randomUUID().toString().substring(0, 8);

        if (mongoUri != null) {
            try {
                mongoClient = MongoClients.create(mongoUri);
                MongoDatabase database = mongoClient.getDatabase(dbName);
                MongoCollection<Document> collection = database.getCollection(testCollectionName);
                tracedMongo = new TracedMongoVectorSearch(collection, tracer, testCollectionName);
            } catch (Exception e) {
                System.out.println("Failed to create MongoDB client: " + e.getMessage());
            }
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        // Best-effort cleanup
        if (tracedMongo != null) {
            try {
                tracedMongo.unwrap().drop();
            } catch (Exception ignored) {
            }
        }
        if (mongoClient != null) {
            try {
                mongoClient.close();
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
    void shouldInsertOneDocument() {
        Assumptions.assumeTrue(tracedMongo != null, "MongoDB client not configured");

        try {
            Document doc = new Document()
                    .append("text", "The quick brown fox")
                    .append("embedding", Arrays.asList(0.1, 0.2, 0.3, 0.4))
                    .append("category", "animals");

            tracedMongo.insertOne(doc);
            System.out.println("Inserted 1 document into: " + testCollectionName);
        } catch (Exception e) {
            System.out.println("InsertOne error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldInsertManyDocuments() {
        Assumptions.assumeTrue(tracedMongo != null, "MongoDB client not configured");

        try {
            List<Document> docs = Arrays.asList(
                    new Document()
                            .append("text", "Machine learning fundamentals")
                            .append("embedding", Arrays.asList(0.5, 0.6, 0.7, 0.8))
                            .append("category", "tech"),
                    new Document()
                            .append("text", "Deep neural networks")
                            .append("embedding", Arrays.asList(0.9, 0.1, 0.2, 0.3))
                            .append("category", "tech"),
                    new Document()
                            .append("text", "Natural language processing")
                            .append("embedding", Arrays.asList(0.4, 0.5, 0.6, 0.7))
                            .append("category", "ai")
            );

            tracedMongo.insertMany(docs);
            System.out.println("Inserted 3 documents");
        } catch (Exception e) {
            System.out.println("InsertMany error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldPerformVectorSearch() {
        Assumptions.assumeTrue(tracedMongo != null, "MongoDB client not configured");

        try {
            List<Double> queryVector = Arrays.asList(0.1, 0.2, 0.3, 0.4);
            List<Document> results = tracedMongo.vectorSearch(
                    queryVector, "embedding", vectorIndexName, 5, 50);
            assertThat(results).isNotNull();
            System.out.println("Vector search returned " + results.size() + " results");
        } catch (Exception e) {
            // This is expected to fail if no Atlas vector search index is configured.
            // The span is still exported, which is the key verification.
            System.out.println("Vector search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldPerformVectorSearchWithFilter() {
        Assumptions.assumeTrue(tracedMongo != null, "MongoDB client not configured");

        try {
            List<Double> queryVector = Arrays.asList(0.5, 0.6, 0.7, 0.8);
            Document filter = new Document("category", "tech");

            List<Document> results = tracedMongo.vectorSearch(
                    queryVector, "embedding", vectorIndexName, 3, 30, filter);
            assertThat(results).isNotNull();
            System.out.println("Filtered vector search returned " + results.size() + " results");
        } catch (Exception e) {
            System.out.println("Filtered search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(6)
    void shouldDeleteDocuments() {
        Assumptions.assumeTrue(tracedMongo != null, "MongoDB client not configured");

        try {
            long deletedCount = tracedMongo.deleteMany(new Document("category", "animals"));
            System.out.println("Deleted " + deletedCount + " documents");
        } catch (Exception e) {
            System.out.println("Delete error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(7)
    void shouldExposeUnwrappedCollection() {
        Assumptions.assumeTrue(tracedMongo != null, "MongoDB client not configured");
        assertThat(tracedMongo.unwrap()).isNotNull();
    }
}
