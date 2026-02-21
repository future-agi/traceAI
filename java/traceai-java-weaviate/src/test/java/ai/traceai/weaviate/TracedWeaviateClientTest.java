package ai.traceai.weaviate;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.weaviate.client.WeaviateClient;
import io.weaviate.client.base.Result;
import io.weaviate.client.v1.batch.api.ObjectsBatcher;
import io.weaviate.client.v1.batch.Batch;
import io.weaviate.client.v1.batch.model.ObjectGetResponse;
import io.weaviate.client.v1.data.Data;
import io.weaviate.client.v1.data.api.ObjectCreator;
import io.weaviate.client.v1.data.api.ObjectDeleter;
import io.weaviate.client.v1.data.api.ObjectsGetter;
import io.weaviate.client.v1.data.model.WeaviateObject;
import io.weaviate.client.v1.graphql.GraphQL;
import io.weaviate.client.v1.graphql.model.GraphQLResponse;
import io.weaviate.client.v1.graphql.query.Get;
import io.weaviate.client.v1.graphql.query.fields.Field;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class TracedWeaviateClientTest {

    @RegisterExtension
    static final OpenTelemetryExtension otelTesting = OpenTelemetryExtension.create();

    @Mock
    private WeaviateClient mockClient;

    private FITracer tracer;

    @BeforeEach
    void setup() {
        tracer = new FITracer(otelTesting.getOpenTelemetry().getTracer("test"));
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldCreateSpanForNearVectorSearch() throws Exception {
        // Mock the GraphQL chain: client.graphQL().get().withClassName().withFields().withNearVector().withLimit().run()
        GraphQL mockGraphQL = mock(GraphQL.class);
        Get mockGet = mock(Get.class);
        Result<GraphQLResponse> mockResult = mock(Result.class);

        when(mockClient.graphQL()).thenReturn(mockGraphQL);
        when(mockGraphQL.get()).thenReturn(mockGet);
        when(mockGet.withClassName(any())).thenReturn(mockGet);
        when(mockGet.withFields(any(Field[].class))).thenReturn(mockGet);
        when(mockGet.withNearVector(any())).thenReturn(mockGet);
        when(mockGet.withLimit(anyInt())).thenReturn(mockGet);
        when(mockGet.run()).thenReturn(mockResult);
        when(mockResult.hasErrors()).thenReturn(false);
        when(mockResult.getResult()).thenReturn(null);

        TracedWeaviateClient traced = new TracedWeaviateClient(mockClient, tracer);

        Float[] queryVector = new Float[]{0.1f, 0.2f, 0.3f, 0.4f};
        Result<GraphQLResponse> response = traced.nearVectorSearch("Article", queryVector, 10, "title", "content");

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Weaviate NearVector Search");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("weaviate");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.class")))
            .isEqualTo("Article");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.RETRIEVER_TOP_K)))
            .isEqualTo(10L);
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(4L);
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldCreateSpanForCreateObject() throws Exception {
        // Mock the data chain: client.data().creator().withClassName().withProperties().withVector().run()
        Data mockData = mock(Data.class);
        ObjectCreator mockCreator = mock(ObjectCreator.class);
        Result<WeaviateObject> mockResult = mock(Result.class);

        when(mockClient.data()).thenReturn(mockData);
        when(mockData.creator()).thenReturn(mockCreator);
        when(mockCreator.withClassName(any())).thenReturn(mockCreator);
        when(mockCreator.withProperties(any())).thenReturn(mockCreator);
        when(mockCreator.withVector(any(Float[].class))).thenReturn(mockCreator);
        when(mockCreator.run()).thenReturn(mockResult);
        when(mockResult.hasErrors()).thenReturn(false);
        when(mockResult.getResult()).thenReturn(null);

        TracedWeaviateClient traced = new TracedWeaviateClient(mockClient, tracer);

        Map<String, Object> properties = new HashMap<>();
        properties.put("title", "Test Document");
        Float[] vector = new Float[]{0.1f, 0.2f, 0.3f};

        Result<WeaviateObject> response = traced.createObject("Article", properties, vector);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Weaviate Create Object");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("weaviate");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.class")))
            .isEqualTo("Article");
        assertThat(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.EMBEDDING_DIMENSIONS)))
            .isEqualTo(3L);
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldCreateSpanForBatchImport() throws Exception {
        // Mock the batch chain: client.batch().objectsBatcher().withObjects().run()
        Batch mockBatch = mock(Batch.class);
        ObjectsBatcher mockBatcher = mock(ObjectsBatcher.class);
        Result<ObjectGetResponse[]> mockResult = mock(Result.class);

        when(mockClient.batch()).thenReturn(mockBatch);
        when(mockBatch.objectsBatcher()).thenReturn(mockBatcher);
        when(mockBatcher.withObjects(any(WeaviateObject[].class))).thenReturn(mockBatcher);
        when(mockBatcher.run()).thenReturn(mockResult);
        when(mockResult.hasErrors()).thenReturn(false);
        when(mockResult.getResult()).thenReturn(new ObjectGetResponse[]{});

        WeaviateObject obj = WeaviateObject.builder().className("Article").build();
        TracedWeaviateClient traced = new TracedWeaviateClient(mockClient, tracer);

        Result<ObjectGetResponse[]> response = traced.batchImport(obj);

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Weaviate Batch Import");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.EMBEDDING.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("weaviate");
        assertThat(span.getAttributes().get(AttributeKey.longKey("weaviate.batch_size")))
            .isEqualTo(1L);
        assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.class")))
            .isEqualTo("Article");
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldCreateSpanForDeleteObject() throws Exception {
        // Mock the data chain: client.data().deleter().withClassName().withID().run()
        Data mockData = mock(Data.class);
        ObjectDeleter mockDeleter = mock(ObjectDeleter.class);
        Result<Boolean> mockResult = mock(Result.class);

        when(mockClient.data()).thenReturn(mockData);
        when(mockData.deleter()).thenReturn(mockDeleter);
        when(mockDeleter.withClassName(any())).thenReturn(mockDeleter);
        when(mockDeleter.withID(any())).thenReturn(mockDeleter);
        when(mockDeleter.run()).thenReturn(mockResult);
        when(mockResult.hasErrors()).thenReturn(false);

        TracedWeaviateClient traced = new TracedWeaviateClient(mockClient, tracer);

        Result<Boolean> response = traced.deleteObject("Article", "obj-123");

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Weaviate Delete Object");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("weaviate");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.class")))
            .isEqualTo("Article");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.object_id")))
            .isEqualTo("obj-123");
    }

    @Test
    @SuppressWarnings("unchecked")
    void shouldCreateSpanForGetObject() throws Exception {
        // Mock the data chain: client.data().objectsGetter().withClassName().withID().run()
        Data mockData = mock(Data.class);
        ObjectsGetter mockGetter = mock(ObjectsGetter.class);
        Result<List<WeaviateObject>> mockResult = mock(Result.class);

        when(mockClient.data()).thenReturn(mockData);
        when(mockData.objectsGetter()).thenReturn(mockGetter);
        when(mockGetter.withClassName(any())).thenReturn(mockGetter);
        when(mockGetter.withID(any())).thenReturn(mockGetter);
        when(mockGetter.run()).thenReturn(mockResult);
        when(mockResult.hasErrors()).thenReturn(false);
        when(mockResult.getResult()).thenReturn(List.of());

        TracedWeaviateClient traced = new TracedWeaviateClient(mockClient, tracer);

        Result<List<WeaviateObject>> response = traced.getObject("Article", "obj-123");

        assertThat(response).isNotNull();

        List<SpanData> spans = otelTesting.getSpans();
        assertThat(spans).hasSize(1);

        SpanData span = spans.get(0);
        assertThat(span.getName()).isEqualTo("Weaviate Get Object");
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.FI_SPAN_KIND)))
            .isEqualTo(FISpanKind.RETRIEVER.getValue());
        assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.LLM_SYSTEM)))
            .isEqualTo("weaviate");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.class")))
            .isEqualTo("Article");
        assertThat(span.getAttributes().get(AttributeKey.stringKey("weaviate.object_id")))
            .isEqualTo("obj-123");
    }

    @Test
    void shouldReturnUnwrappedClient() {
        TracedWeaviateClient traced = new TracedWeaviateClient(mockClient, tracer);
        assertThat(traced.unwrap()).isSameAs(mockClient);
    }
}
