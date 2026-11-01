package ai.traceai.redis;

import ai.traceai.FITracer;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.trace.Span;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import redis.clients.jedis.CommandArguments;
import redis.clients.jedis.JedisPooled;
import redis.clients.jedis.Protocol;
import redis.clients.jedis.args.Rawable;
import redis.clients.jedis.search.FTCreateParams;
import redis.clients.jedis.search.Query;
import redis.clients.jedis.search.RediSearchUtil;
import redis.clients.jedis.search.SearchResult;
import redis.clients.jedis.search.schemafields.SchemaField;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.*;

class TracedRedisVectorSearchTest {
    private final RecordingJedis jedis = new RecordingJedis();
    private final TracedRedisVectorSearch traced = new TracedRedisVectorSearch(
            jedis, new FITracer(OpenTelemetry.noop().getTracer("redis-test")));

    @AfterEach
    void close() {
        jedis.close();
    }

    @Test
    void unfilteredSearchUsesTopKAsPageSize() {
        traced.vectorSearch("index", new float[]{1, 2}, 25);
        assertThat(arguments(jedis.query)).containsSubsequence("LIMIT", "0", "25");
    }

    @Test
    void filteredSearchUsesTopKAsPageSize() {
        traced.vectorSearch("index", new float[]{1, 2}, 25, "@category:{tech}");
        assertThat(arguments(jedis.query)).containsSubsequence("LIMIT", "0", "25");
    }

    @ParameterizedTest
    @ValueSource(ints = {1, 7, 10, 100})
    void bothSearchPathsRespectRequestedLimit(int topK) {
        for (String filter : new String[]{null, "@category:{tech}"}) {
            traced.vectorSearch("index", new float[]{1, -2}, topK, filter);
            List<String> args = arguments(jedis.query);
            // Jedis elides LIMIT 0 10 because it is the Redis default.
            if (topK != 10) {
                assertThat(args).containsSubsequence("LIMIT", "0", Integer.toString(topK));
            } else {
                assertThat(args).doesNotContain("LIMIT");
            }
            assertThat(args).containsSubsequence("SORTBY", "score", "ASC")
                    .containsSubsequence("RETURN", "1", "score")
                    .containsSubsequence("DIALECT", "2");
            List<byte[]> bytes = rawArguments(jedis.query);
            assertThat(bytes.get(args.indexOf("BLOB") + 1))
                    .isEqualTo(ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN)
                            .putFloat(1).putFloat(-2).array());
        }
    }

    @Test
    void createdCustomFieldIsUsedByLegacySearch() {
        traced.createIndex("index", "custom_vector", 2, "COSINE", "HNSW");
        traced.vectorSearch("index", new float[]{1, 2}, 25);
        assertThat(arguments(jedis.query).get(1))
                .isEqualTo("*=>[KNN 25 @custom_vector $BLOB AS score]");
        traced.vectorSearch("index", new float[]{1, 2}, 25, "@category:{tech}");
        assertThat(arguments(jedis.query).get(1))
                .isEqualTo("(@category:{tech})=>[KNN 25 @custom_vector $BLOB AS score]");
    }

    @Test
    void createdCustomFieldIsUsedByLegacyDocumentWrite() {
        traced.createIndex("index", "custom_vector", 2, "COSINE", "HNSW");
        traced.addDocument("doc:café", new float[]{1, -2}, Map.of("title", "こんにちは"));
        assertThat(jedis.hash).containsOnlyKeys("custom_vector", "title");
        assertThat(jedis.hash.get("custom_vector")).isEqualTo(RediSearchUtil.toByteArray(new float[]{1, -2}));
        assertThat(jedis.hash.get("title")).isEqualTo("こんにちは".getBytes(StandardCharsets.UTF_8));
        assertThat(jedis.key).isEqualTo("doc:café".getBytes(StandardCharsets.UTF_8));
    }

    @Test
    void legacyDefaultsStillUseEmbedding() {
        traced.addDocument("doc:1", new float[]{1}, null);
        assertThat(jedis.hash).containsOnlyKeys("embedding");
        traced.vectorSearch("external-index", new float[]{1}, 5);
        assertThat(arguments(jedis.query).get(1)).contains("@embedding ");
        assertThat(traced.unwrap()).isSameAs(jedis);
    }

    @Test
    void explicitFieldWorksForExternalIndexesAndDoesNotChangeOtherIndexes() {
        traced.createIndex("first", "first_vector", 2, "COSINE", "FLAT");
        traced.createIndex("second", "second_vector", 2, "COSINE", "FLAT");
        traced.vectorSearch("first", new float[]{1, 2}, 25);
        assertThat(arguments(jedis.query).get(1)).contains("@first_vector ");
        traced.vectorSearch("second", new float[]{1, 2}, 25);
        assertThat(arguments(jedis.query).get(1)).contains("@second_vector ");
        traced.vectorSearch("external", "external_vector", new float[]{1, 2}, 25, "@category:{tech}");
        assertThat(arguments(jedis.query).get(1)).contains("@external_vector ");
        traced.vectorSearch("external", "external_vector", new float[]{1, 2}, 25);
        assertThat(arguments(jedis.query).get(1)).startsWith("*=>");
        // Legacy writes must still target embedding when indexes disagree.
        traced.addDocument("doc:1", new float[]{1, 2}, null);
        assertThat(jedis.hash).containsOnlyKeys("embedding");
        assertThat(jedis.writeCalls).isEqualTo(1);
        traced.addDocument("doc:1", "external_vector", new float[]{1, 2}, null);
        assertThat(jedis.hash).containsOnlyKeys("external_vector");
    }

    @Test
    void legacyWriteStillTargetsEmbeddingWithMixedIndexes() {
        traced.createIndex("old-index", "embedding", 2, "COSINE", "FLAT");
        traced.createIndex("new-index", "custom_vector", 2, "COSINE", "FLAT");
        traced.addDocument("doc:legacy", new float[]{1, 2}, null);
        assertThat(jedis.hash).containsOnlyKeys("embedding");
        traced.addDocument("doc:custom", "custom_vector", new float[]{1, 2}, null);
        assertThat(jedis.hash).containsOnlyKeys("custom_vector");
    }

    @Test
    void genericQueryErrorsDoNotClaimVectorSearch() {
        jedis.failure = new IllegalStateException("search failed");
        assertThatThrownBy(() -> traced.search("index", new Query("*")))
                .isInstanceOf(RuntimeException.class)
                .hasMessage("Redis search failed")
                .hasCause(jedis.failure);
    }

    @Test
    void customFieldNamesAreEscapedOnlyInQuerySyntax() {
        String field = "product-vector name\\suffix";
        traced.createIndex("index", field, 2, "COSINE", "FLAT");
        assertThat(jedis.schema).contains(field);
        traced.addDocument("doc:1", new float[]{1, 2}, null);
        assertThat(jedis.hash).containsOnlyKeys(field);
        traced.vectorSearch("index", new float[]{1, 2}, 25);
        assertThat(arguments(jedis.query).get(1))
                .isEqualTo("*=>[KNN 25 @product\\-vector\\ name\\\\suffix $BLOB AS score]");
    }

    @Test
    void failedIndexCreationDoesNotChangeFieldDefaults() {
        jedis.failure = new IllegalStateException("create failed");
        assertThatThrownBy(() -> traced.createIndex("index", "wrong_vector", 2, "COSINE", "FLAT"))
                .hasCause(jedis.failure);
        jedis.failure = null;
        traced.vectorSearch("index", new float[]{1, 2}, 25);
        assertThat(arguments(jedis.query).get(1)).contains("@embedding ");
        traced.addDocument("doc:1", new float[]{1, 2}, null);
        assertThat(jedis.hash).containsOnlyKeys("embedding");
    }

    @ParameterizedTest
    @ValueSource(strings = {"0.25", "not-a-number"})
    void optionalScoreTelemetryDoesNotChangeReturnedDocuments(String score) {
        jedis.result = new SearchResult.SearchResultBuilder(true, false, true).build(List.of(
                1L, "doc:1".getBytes(StandardCharsets.UTF_8),
                List.of("score".getBytes(StandardCharsets.UTF_8), score.getBytes(StandardCharsets.UTF_8))));
        assertThat(traced.vectorSearch("index", new float[]{1, 2}, 25)).isSameAs(jedis.result);
        assertThat(traced.vectorSearch("index", new float[]{1, 2}, 25, "@category:{tech}"))
                .isSameAs(jedis.result);
    }

    @Test
    void delegatesNativeQueriesWithoutMutatingOrReplacingThem() {
        Query query = new Query("@title:$title").addParam("title", "hello")
                .limit(5, 30).returnFields("title").dialect(2);
        List<String> before = arguments(query);
        assertThat(traced.search("index", query)).isSameAs(jedis.result);
        assertThat(jedis.query).isSameAs(query);
        assertThat(arguments(query)).isEqualTo(before);
        assertThat(jedis.searchCalls).isEqualTo(1);
    }

    @ParameterizedTest
    @ValueSource(strings = {"COSINE", "L2", "IP", "cosine"})
    void createsValidFloat32Schema(String metric) {
        for (String algorithm : List.of("FLAT", "HNSW", "hnsw")) {
            traced.createIndex("index", "embedding", 2, metric, algorithm);
            assertThat(jedis.schema).containsSubsequence("embedding", "VECTOR", algorithm.toUpperCase(), "6")
                    .containsSubsequence("TYPE", "FLOAT32")
                    .containsSubsequence("DIM", "2")
                    .containsSubsequence("DISTANCE_METRIC", metric.toUpperCase());
            assertThat(jedis.createParams).containsSubsequence("PREFIX", "1", "doc:");
        }
    }

    @Test
    void invalidDistanceMetricIsRejectedBeforeRedis() {
        assertThatThrownBy(() -> traced.createIndex("index", "embedding", 2, "FLOAT32", "FLAT"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThat(jedis.createCalls).isZero();
    }

    @Test
    void invalidAlgorithmIsRejectedBeforeRedis() {
        assertThatThrownBy(() -> traced.createIndex("index", "embedding", 2, "COSINE", "TYPO"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThat(jedis.createCalls).isZero();
    }

    @Test
    void invalidArgumentsNeverReachRedis() {
        assertThatThrownBy(() -> traced.createIndex("index", "embedding", 0, "COSINE", "FLAT"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.createIndex("index", "embedding", 2, null, "FLAT"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.createIndex("index", "embedding", 2, "COSINE", null))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.vectorSearch("index", new float[]{1}, 0))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.vectorSearch("index", new float[]{1}, -1))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.vectorSearch("index", new float[0], 1))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.vectorSearch("index", null, 1))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.vectorSearch("index", new float[]{Float.NaN}, 1))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.vectorSearch("index", "", new float[]{1}, 1))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.vectorSearch(null, new float[]{1}, 1))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.addDocument("doc:1", new float[]{1}, Map.of("embedding", "oops")))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> traced.addDocument("doc:1", "custom", new float[]{1}, Map.of("custom", "oops")))
                .isInstanceOf(IllegalArgumentException.class);
        assertThat(jedis.createCalls + jedis.searchCalls + jedis.writeCalls).isZero();
    }

    @Test
    void blankFilterUsesUnfilteredQuery() {
        traced.vectorSearch("index", new float[]{1}, 5, "  ");
        assertThat(arguments(jedis.query).get(1)).startsWith("*=>");
    }

    static List<byte[]> rawArguments(Query query) {
        CommandArguments args = new CommandArguments(Protocol.Command.GET);
        query.addParams(args);
        List<byte[]> result = new ArrayList<>();
        args.forEach(arg -> result.add(arg.getRaw()));
        return result;
    }

    static List<String> arguments(Query query) {
        return rawArguments(query).stream().map(bytes -> new String(bytes, StandardCharsets.UTF_8)).toList();
    }

    static List<String> strings(Iterable<Rawable> args) {
        List<String> result = new ArrayList<>();
        args.forEach(arg -> result.add(new String(arg.getRaw(), StandardCharsets.UTF_8)));
        return result;
    }

    static class RecordingJedis extends JedisPooled {
        Query query;
        byte[] key;
        Map<String, byte[]> hash;
        List<String> schema;
        List<String> createParams;
        int createCalls;
        int searchCalls;
        int writeCalls;
        RuntimeException failure;
        String currentSpanId;
        SearchResult result = new SearchResult.SearchResultBuilder(true, false, true)
                .build(List.of(0L));

        RecordingJedis() {
            super("localhost", 6379);
        }

        @Override
        public SearchResult ftSearch(String indexName, Query query) {
            searchCalls++;
            this.query = query;
            currentSpanId = Span.current().getSpanContext().getSpanId();
            if (failure != null) {
                throw failure;
            }
            return result;
        }

        @Override
        public String ftCreate(String indexName, FTCreateParams params, Iterable<SchemaField> fields) {
            createCalls++;
            if (failure != null) {
                throw failure;
            }
            CommandArguments args = new CommandArguments(Protocol.Command.GET);
            for (SchemaField field : fields) {
                field.addParams(args);
            }
            schema = strings(args);
            args = new CommandArguments(Protocol.Command.GET);
            params.addParams(args);
            createParams = strings(args);
            return "OK";
        }

        @Override
        public long hset(byte[] key, Map<byte[], byte[]> hash) {
            writeCalls++;
            this.key = key;
            this.hash = new LinkedHashMap<>();
            hash.forEach((field, value) -> this.hash.put(new String(field, StandardCharsets.UTF_8), value));
            return hash.size();
        }

        @Override
        public long del(String key) {
            return 1;
        }
    }
}
