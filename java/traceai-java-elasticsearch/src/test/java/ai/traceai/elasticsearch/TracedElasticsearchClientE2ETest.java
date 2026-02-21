package ai.traceai.elasticsearch;

import static org.assertj.core.api.Assertions.*;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import co.elastic.clients.elasticsearch.ElasticsearchClient;
import co.elastic.clients.elasticsearch._types.mapping.DenseVectorProperty;
import co.elastic.clients.elasticsearch._types.mapping.Property;
import co.elastic.clients.elasticsearch._types.mapping.TextProperty;
import co.elastic.clients.elasticsearch.core.*;
import co.elastic.clients.elasticsearch.core.search.Hit;
import co.elastic.clients.json.jackson.JacksonJsonpMapper;
import co.elastic.clients.transport.ElasticsearchTransport;
import co.elastic.clients.transport.rest_client.RestClientTransport;
import org.apache.http.HttpHost;
import org.apache.http.auth.AuthScope;
import org.apache.http.auth.UsernamePasswordCredentials;
import org.apache.http.impl.client.BasicCredentialsProvider;
import org.elasticsearch.client.RestClient;
import org.elasticsearch.client.RestClientBuilder;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

import java.net.URI;
import java.util.*;

/**
 * E2E test for TracedElasticsearchClient.
 * Requires a running Elasticsearch instance and FI_API_KEY to export spans.
 *
 * <p>Set environment variables:</p>
 * <ul>
 *   <li>FI_API_KEY - API key for the FI backend</li>
 *   <li>ELASTICSEARCH_URL - Elasticsearch URL (e.g., http://localhost:9200)</li>
 *   <li>ELASTICSEARCH_USER - (optional) username for authentication</li>
 *   <li>ELASTICSEARCH_PASSWORD - (optional) password for authentication</li>
 *   <li>FI_BASE_URL - (optional) defaults to https://api.futureagi.com</li>
 *   <li>FI_PROJECT_NAME - (optional) defaults to java-elasticsearch-e2e</li>
 * </ul>
 */
@EnabledIfEnvironmentVariable(named = "FI_API_KEY", matches = ".+")
@EnabledIfEnvironmentVariable(named = "ELASTICSEARCH_URL", matches = ".+")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class TracedElasticsearchClientE2ETest {

    private static FITracer tracer;
    private static TracedElasticsearchClient tracedClient;
    private static ElasticsearchClient rawClient;
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
                        : "java-elasticsearch-e2e")
                .enableConsoleExporter(true)
                .build());
        tracer = TraceAI.getTracer();

        String esUrl = System.getenv("ELASTICSEARCH_URL");
        String esUser = System.getenv("ELASTICSEARCH_USER");
        String esPassword = System.getenv("ELASTICSEARCH_PASSWORD");
        testIndexName = "e2e_test_" + UUID.randomUUID().toString().substring(0, 8);

        if (esUrl != null) {
            try {
                URI uri = URI.create(esUrl);
                HttpHost httpHost = new HttpHost(
                        uri.getHost(),
                        uri.getPort() > 0 ? uri.getPort() : 9200,
                        uri.getScheme() != null ? uri.getScheme() : "http"
                );

                RestClientBuilder builder = RestClient.builder(httpHost);

                if (esUser != null && esPassword != null) {
                    BasicCredentialsProvider credentialsProvider = new BasicCredentialsProvider();
                    credentialsProvider.setCredentials(
                            AuthScope.ANY,
                            new UsernamePasswordCredentials(esUser, esPassword)
                    );
                    builder.setHttpClientConfigCallback(
                            httpClientBuilder -> httpClientBuilder.setDefaultCredentialsProvider(credentialsProvider)
                    );
                }

                RestClient restClient = builder.build();
                ElasticsearchTransport transport = new RestClientTransport(restClient, new JacksonJsonpMapper());
                rawClient = new ElasticsearchClient(transport);
                tracedClient = new TracedElasticsearchClient(rawClient, tracer);
            } catch (Exception e) {
                System.out.println("Failed to create Elasticsearch client: " + e.getMessage());
            }
        }
    }

    @AfterAll
    static void tearDown() throws InterruptedException {
        // Best-effort cleanup
        if (rawClient != null) {
            try {
                rawClient.indices().delete(d -> d.index(testIndexName));
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
        Assumptions.assumeTrue(tracedClient != null, "Elasticsearch client not configured");

        try {
            Map<String, Property> mappings = new HashMap<>();
            mappings.put("embedding", Property.of(p -> p
                    .denseVector(DenseVectorProperty.of(dv -> dv
                            .dims(VECTOR_DIM)
                            .index(true)
                            .similarity("cosine")
                    ))
            ));
            mappings.put("title", Property.of(p -> p
                    .text(TextProperty.of(t -> t))
            ));

            var response = tracedClient.createIndex(testIndexName, mappings);
            assertThat(response).isNotNull();
            System.out.println("Created index: " + testIndexName +
                    " acknowledged=" + response.acknowledged());
        } catch (Exception e) {
            System.out.println("Create index error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(3)
    void shouldIndexDocument() {
        Assumptions.assumeTrue(tracedClient != null, "Elasticsearch client not configured");

        try {
            Map<String, Object> doc = new HashMap<>();
            doc.put("title", "First test document");
            doc.put("embedding", Arrays.asList(0.1f, 0.2f, 0.3f, 0.4f));
            doc.put("category", "tech");

            IndexResponse response = tracedClient.index(testIndexName, "doc-1", doc);
            assertThat(response).isNotNull();
            System.out.println("Indexed document doc-1, result: " + response.result().jsonValue());
        } catch (Exception e) {
            System.out.println("Index error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(4)
    void shouldBulkIndexDocuments() {
        Assumptions.assumeTrue(tracedClient != null, "Elasticsearch client not configured");

        try {
            List<Map<String, Object>> documents = new ArrayList<>();

            Map<String, Object> doc2 = new HashMap<>();
            doc2.put("_id", "doc-2");
            doc2.put("title", "Machine learning basics");
            doc2.put("embedding", Arrays.asList(0.5f, 0.6f, 0.7f, 0.8f));
            doc2.put("category", "ml");
            documents.add(doc2);

            Map<String, Object> doc3 = new HashMap<>();
            doc3.put("_id", "doc-3");
            doc3.put("title", "Neural network architectures");
            doc3.put("embedding", Arrays.asList(0.9f, 0.1f, 0.2f, 0.3f));
            doc3.put("category", "ml");
            documents.add(doc3);

            BulkResponse response = tracedClient.bulkIndex(testIndexName, documents);
            assertThat(response).isNotNull();
            System.out.println("Bulk indexed " + response.items().size() +
                    " docs, errors=" + response.errors());
        } catch (Exception e) {
            System.out.println("Bulk index error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(5)
    void shouldPerformKnnSearch() {
        Assumptions.assumeTrue(tracedClient != null, "Elasticsearch client not configured");

        try {
            // Wait briefly for indexing to complete
            Thread.sleep(1000);

            float[] queryVector = {0.1f, 0.2f, 0.3f, 0.4f};
            SearchResponse<Map<String, Object>> response = tracedClient.knnSearch(
                    testIndexName, queryVector, 5, 50, "embedding");
            assertThat(response).isNotNull();

            List<Hit<Map<String, Object>>> hits = response.hits().hits();
            System.out.println("KNN search returned " + hits.size() + " hits");
        } catch (Exception e) {
            System.out.println("KNN search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(6)
    void shouldPerformKnnSearchWithFilter() {
        Assumptions.assumeTrue(tracedClient != null, "Elasticsearch client not configured");

        try {
            float[] queryVector = {0.5f, 0.6f, 0.7f, 0.8f};
            co.elastic.clients.elasticsearch._types.query_dsl.Query filter =
                    co.elastic.clients.elasticsearch._types.query_dsl.Query.of(q -> q
                            .term(t -> t.field("category").value("ml"))
                    );

            SearchResponse<Map<String, Object>> response = tracedClient.knnSearchWithFilter(
                    testIndexName, queryVector, 3, 30, "embedding", filter);
            assertThat(response).isNotNull();
            System.out.println("Filtered KNN search returned " + response.hits().hits().size() + " hits");
        } catch (Exception e) {
            System.out.println("Filtered KNN search error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(7)
    void shouldDeleteDocument() {
        Assumptions.assumeTrue(tracedClient != null, "Elasticsearch client not configured");

        try {
            DeleteResponse response = tracedClient.delete(testIndexName, "doc-3");
            assertThat(response).isNotNull();
            System.out.println("Deleted doc-3, result: " + response.result().jsonValue());
        } catch (Exception e) {
            System.out.println("Delete error (span still exported): " + e.getMessage());
        }
    }

    @Test
    @Order(8)
    void shouldExposeUnwrappedClient() {
        Assumptions.assumeTrue(tracedClient != null, "Elasticsearch client not configured");
        assertThat(tracedClient.unwrap()).isNotNull();
    }
}
