package ai.traceai.chromadb;

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
import tech.amikos.chromadb.Collection;

import java.util.Arrays;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedChromaCollectionTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private Collection mockCollection;

    @Mock
    private Collection.QueryResponse mockQueryResponse;

    @Mock
    private Collection.GetResult mockGetResult;

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    void shouldCreateSpanForQuery() throws Exception {
        List<String> queryTexts = Arrays.asList("what is machine learning?");

        when(mockQueryResponse.getIds()).thenReturn(List.of(List.of("id-1", "id-2")));
        when(mockQueryResponse.getDistances()).thenReturn(List.of(List.of(0.1f, 0.5f)));
        when(mockCollection.query(anyList(), anyInt(), isNull(), isNull(), isNull()))
            .thenReturn(mockQueryResponse);

        TracedChromaCollection traced = new TracedChromaCollection(mockCollection, tracer, "test-collection");

        Collection.QueryResponse response = traced.query(queryTexts, 10, null, null, null);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("ChromaDB Query");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("chromadb");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("chromadb.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(AttributeKey.longKey("chromadb.query_count")))
            .isEqualTo(1L);
        assertThat(span.getAttributes().get(AttributeKey.longKey("chromadb.results_count")))
            .isEqualTo(2L);
    }

    @Test
    void shouldCreateSpanForAdd() throws Exception {
        List<String> ids = Arrays.asList("id-1", "id-2");
        List<String> documents = Arrays.asList("doc one", "doc two");

        when(mockCollection.add(isNull(), isNull(), anyList(), anyList())).thenReturn(null);

        TracedChromaCollection traced = new TracedChromaCollection(mockCollection, tracer, "test-collection");

        traced.add(null, null, documents, ids);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("ChromaDB Add");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("chromadb");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("chromadb.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey("chromadb.add_count")))
            .isEqualTo(2L);
    }

    @Test
    void shouldCreateSpanForDelete() throws Exception {
        List<String> ids = Arrays.asList("id-1", "id-2");

        when(mockCollection.delete(anyList(), isNull(), isNull())).thenReturn(null);

        TracedChromaCollection traced = new TracedChromaCollection(mockCollection, tracer, "test-collection");

        traced.delete(ids, null, null);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("ChromaDB Delete");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("chromadb");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("chromadb.collection")))
            .isEqualTo("test-collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey("chromadb.delete_ids_count")))
            .isEqualTo(2L);
    }

    @Test
    void shouldCreateSpanForCount() throws Exception {
        when(mockCollection.count()).thenReturn(42);

        TracedChromaCollection traced = new TracedChromaCollection(mockCollection, tracer, "test-collection");

        int count = traced.count();

        assertThat(count).isEqualTo(42);

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("ChromaDB Count");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("chromadb");
        assertThat(span.getAttributes().get(AttributeKey.longKey("chromadb.count")))
            .isEqualTo(42L);
    }

    @Test
    void shouldReturnUnwrapped() {
        TracedChromaCollection traced = new TracedChromaCollection(mockCollection, tracer, "test-collection");
        assertThat(traced.unwrap()).isSameAs(mockCollection);
    }
}
