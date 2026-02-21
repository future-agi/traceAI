package ai.traceai.redis;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import redis.clients.jedis.JedisPooled;
import redis.clients.jedis.search.Document;
import redis.clients.jedis.search.Query;
import redis.clients.jedis.search.SearchResult;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doNothing;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedRedisVectorSearchTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private JedisPooled mockJedis;

    @Mock
    private SearchResult mockSearchResult;

    @Mock
    private Document mockDocument;

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    void shouldCreateSpanForVectorSearch() {
        float[] queryVector = new float[]{0.1f, 0.2f, 0.3f, 0.4f};

        when(mockDocument.get("score")).thenReturn("0.85");
        when(mockSearchResult.getTotalResults()).thenReturn(5L);
        when(mockSearchResult.getDocuments()).thenReturn(List.of(mockDocument));
        when(mockJedis.ftSearch(anyString(), any(Query.class)))
            .thenReturn(mockSearchResult);

        TracedRedisVectorSearch traced = new TracedRedisVectorSearch(mockJedis, tracer);

        SearchResult result = traced.vectorSearch("test-index", queryVector, 10);

        assertThat(result).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Redis Vector Search");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("redis");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("redis.index")))
            .isEqualTo("test-index");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(4L);
        assertThat(span.getAttributes().get(AttributeKey.longKey("redis.results_count")))
            .isEqualTo(5L);
    }

    @Test
    void shouldCreateSpanForVectorSearchWithFilter() {
        float[] queryVector = new float[]{0.1f, 0.2f, 0.3f};
        String filter = "@category:{tech}";

        when(mockSearchResult.getTotalResults()).thenReturn(3L);
        when(mockSearchResult.getDocuments()).thenReturn(List.of());
        when(mockJedis.ftSearch(anyString(), any(Query.class)))
            .thenReturn(mockSearchResult);

        TracedRedisVectorSearch traced = new TracedRedisVectorSearch(mockJedis, tracer);

        SearchResult result = traced.vectorSearch("test-index", queryVector, 5, filter);

        assertThat(result).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Redis Vector Search");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey("redis.filter")))
            .isEqualTo("@category:{tech}");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(5L);
    }

    @Test
    void shouldCreateSpanForAddDocument() {
        float[] vector = new float[]{0.1f, 0.2f, 0.3f};
        Map<String, String> metadata = new HashMap<>();
        metadata.put("title", "Test Document");

        when(mockJedis.hset(any(byte[].class), any(Map.class)))
            .thenReturn(2L);

        TracedRedisVectorSearch traced = new TracedRedisVectorSearch(mockJedis, tracer);

        traced.addDocument("doc:1", vector, metadata);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Redis Add Document");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("redis");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("redis.key")))
            .isEqualTo("doc:1");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(3L);
    }

    @Test
    void shouldCreateSpanForDeleteDocument() {
        when(mockJedis.del(anyString())).thenReturn(1L);

        TracedRedisVectorSearch traced = new TracedRedisVectorSearch(mockJedis, tracer);

        traced.deleteDocument("doc:1");

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Redis Delete Document");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("redis");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("redis.key")))
            .isEqualTo("doc:1");
    }

    @Test
    void shouldReturnUnwrapped() {
        TracedRedisVectorSearch traced = new TracedRedisVectorSearch(mockJedis, tracer);
        assertThat(traced.unwrap()).isSameAs(mockJedis);
    }
}
