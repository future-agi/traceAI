package ai.traceai.milvus;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.milvus.v2.client.MilvusClientV2;
import io.milvus.v2.service.vector.request.*;
import io.milvus.v2.service.vector.request.data.BaseVector;
import io.milvus.v2.service.vector.request.data.FloatVec;
import io.milvus.v2.service.vector.response.*;
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

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedMilvusClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private MilvusClientV2 mockClient;

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    void shouldCreateSpanForSearch() {
        SearchResp mockResponse = mock(SearchResp.class);
        List<List<SearchResp.SearchResult>> searchResults = List.of(List.of());
        when(mockResponse.getSearchResults()).thenReturn(searchResults);
        when(mockClient.search(any(SearchReq.class))).thenReturn(mockResponse);

        TracedMilvusClient traced = new TracedMilvusClient(mockClient, tracer);

        List<BaseVector> vectors = List.of(new FloatVec(List.of(0.1f, 0.2f, 0.3f, 0.4f)));
        SearchReq request = SearchReq.builder()
            .collectionName("test_collection")
            .data(vectors)
            .topK(10)
            .build();

        SearchResp response = traced.search(request);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Milvus Search");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("milvus");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("milvus.collection")))
            .isEqualTo("test_collection");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
    }

    @Test
    void shouldCreateSpanForInsert() {
        InsertResp mockResponse = mock(InsertResp.class);
        when(mockResponse.getInsertCnt()).thenReturn(5L);
        when(mockClient.insert(any(InsertReq.class))).thenReturn(mockResponse);

        TracedMilvusClient traced = new TracedMilvusClient(mockClient, tracer);

        InsertReq request = InsertReq.builder()
            .collectionName("test_collection")
            .build();

        InsertResp response = traced.insert(request);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Milvus Insert");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("milvus");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("milvus.collection")))
            .isEqualTo("test_collection");
    }

    @Test
    void shouldCreateSpanForUpsert() {
        UpsertResp mockResponse = mock(UpsertResp.class);
        when(mockResponse.getUpsertCnt()).thenReturn(3L);
        when(mockClient.upsert(any(UpsertReq.class))).thenReturn(mockResponse);

        TracedMilvusClient traced = new TracedMilvusClient(mockClient, tracer);

        UpsertReq request = UpsertReq.builder()
            .collectionName("test_collection")
            .build();

        UpsertResp response = traced.upsert(request);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Milvus Upsert");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("milvus");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("milvus.collection")))
            .isEqualTo("test_collection");
    }

    @Test
    void shouldCreateSpanForDelete() {
        DeleteResp mockResponse = mock(DeleteResp.class);
        when(mockResponse.getDeleteCnt()).thenReturn(2L);
        when(mockClient.delete(any(DeleteReq.class))).thenReturn(mockResponse);

        TracedMilvusClient traced = new TracedMilvusClient(mockClient, tracer);

        DeleteReq request = DeleteReq.builder()
            .collectionName("test_collection")
            .filter("id in [1, 2]")
            .build();

        DeleteResp response = traced.delete(request);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Milvus Delete");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("milvus");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("milvus.collection")))
            .isEqualTo("test_collection");
    }

    @Test
    void shouldCreateSpanForGet() {
        GetResp mockResponse = mock(GetResp.class);
        when(mockResponse.getGetResults()).thenReturn(List.of());
        when(mockClient.get(any(GetReq.class))).thenReturn(mockResponse);

        TracedMilvusClient traced = new TracedMilvusClient(mockClient, tracer);

        GetReq request = GetReq.builder()
            .collectionName("test_collection")
            .ids(List.of("id-1", "id-2"))
            .build();

        GetResp response = traced.get(request);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Milvus Get");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("milvus");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("milvus.collection")))
            .isEqualTo("test_collection");
    }

    @Test
    void shouldCreateSpanForQuery() {
        QueryResp mockResponse = mock(QueryResp.class);
        when(mockResponse.getQueryResults()).thenReturn(List.of());
        when(mockClient.query(any(QueryReq.class))).thenReturn(mockResponse);

        TracedMilvusClient traced = new TracedMilvusClient(mockClient, tracer);

        QueryReq request = QueryReq.builder()
            .collectionName("test_collection")
            .filter("category == 'science'")
            .limit(50)
            .build();

        QueryResp response = traced.query(request);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Milvus Query");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("milvus");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("milvus.collection")))
            .isEqualTo("test_collection");
    }

    @Test
    void shouldReturnUnwrappedClient() {
        TracedMilvusClient traced = new TracedMilvusClient(mockClient, tracer);
        assertThat(traced.unwrap()).isSameAs(mockClient);
    }
}
