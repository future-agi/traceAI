package ai.traceai.pinecone;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.google.protobuf.Struct;
import com.google.protobuf.Value;
import io.grpc.Status;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.pinecone.clients.Index;
import io.pinecone.proto.DeleteResponse;
import io.pinecone.proto.FetchResponse;
import io.pinecone.proto.UpsertResponse;
import io.pinecone.proto.Vector;
import io.pinecone.unsigned_indices_model.QueryResponseWithUnsignedIndices;
import io.pinecone.unsigned_indices_model.VectorWithUnsignedIndices;
import org.assertj.core.api.ThrowableAssert.ThrowingCallable;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowable;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class TracedPineconeIndexTest {
    private final Index index = mock(Index.class);
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private TracedPineconeIndex traced;

    @BeforeEach
    void setUp() {
        provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        traced = new TracedPineconeIndex(index,
                new FITracer(provider.get("pinecone-test"), TraceConfig.builder().build()), "index");
        when(index.query(anyInt(), any(), any(), any(), any(), any(), any(), anyBoolean(), anyBoolean()))
                .thenReturn(mock(QueryResponseWithUnsignedIndices.class));
    }

    @AfterEach
    void tearDown() {
        provider.close();
    }

    @Test
    void nestedFiltersKeepTheirProtobufTypes() {
        traced.query(List.of(1.0f), 3, "namespace", Map.of("$and", List.of(
                Map.of("year", Map.of("$gte", 2020)),
                Map.of("genre", Map.of("$in", List.of("comedy", "drama"))))));
        ArgumentCaptor<Struct> filter = ArgumentCaptor.forClass(Struct.class);
        verify(index).query(eq(3), eq(List.of(1.0f)), isNull(), isNull(), isNull(),
                eq("namespace"), filter.capture(), eq(true), eq(true));
        var clauses = filter.getValue().getFieldsOrThrow("$and").getListValue();
        assertThat(clauses.getValuesCount()).isEqualTo(2);
        assertThat(clauses.getValues(0).getStructValue().getFieldsOrThrow("year")
                .getStructValue().getFieldsOrThrow("$gte").getNumberValue()).isEqualTo(2020);
        assertThat(clauses.getValues(1).getStructValue().getFieldsOrThrow("genre")
                .getStructValue().getFieldsOrThrow("$in").getListValue().getValuesList())
                .extracting(value -> value.getStringValue()).containsExactly("comedy", "drama");
    }

    @Test
    void queryRethrowsTheOriginalSdkException() {
        RuntimeException failure = new IllegalArgumentException("synthetic failure");
        when(index.query(anyInt(), any(), any(), any(), any(), any(), any(), anyBoolean(), anyBoolean()))
                .thenThrow(failure);
        assertThat(catchThrowable(() -> traced.query(List.of(1.0f), 3))).isSameAs(failure);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @ParameterizedTest
    @CsvSource({"false,false", "false,true", "true,false", "true,true"})
    void flagsAndNativeQueryArgumentsAreUnchanged(boolean includeValues, boolean includeMetadata) {
        List<Float> vector = List.of(1.0f);
        List<Long> sparseIndices = List.of(4L, 4294967295L);
        List<Float> sparseValues = List.of(0.5f, 0.7f);
        Struct filter = Struct.getDefaultInstance();
        var response = mock(QueryResponseWithUnsignedIndices.class);
        when(index.query(anyInt(), any(), any(), any(), any(), any(), any(), anyBoolean(), anyBoolean()))
                .thenReturn(response);

        assertThat(traced.query(3, vector, sparseIndices, sparseValues, null, "ns", filter,
                includeValues, includeMetadata)).isSameAs(response);
        verify(index).query(eq(3), same(vector), same(sparseIndices), same(sparseValues), isNull(),
                eq("ns"), same(filter), eq(includeValues), eq(includeMetadata));
        assertThat(traced.query(vector, 4, "ns", Map.of(), includeValues, includeMetadata)).isSameAs(response);
        verify(index).query(eq(4), same(vector), isNull(), isNull(), isNull(), eq("ns"),
                eq(filter), eq(includeValues), eq(includeMetadata));
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
    }

    @Test
    void queryByIdDoesNotRequireADenseVector() {
        traced.query(3, null, null, null, "vector-id", null, null, false, false);
        verify(index).query(3, null, null, null, "vector-id", null, null, false, false);
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @Test
    void conversionPreservesAllJsonTypesAndEmptyContainers() {
        Map<String, Object> filter = new LinkedHashMap<>();
        filter.put("text", "hello");
        filter.put("number", 0.25);
        filter.put("boolean", true);
        filter.put("null", null);
        filter.put("emptyObject", Map.of());
        filter.put("emptyArray", List.of());
        traced.query(List.of(1.0f), 3, null, filter, false, false);
        ArgumentCaptor<Struct> captured = ArgumentCaptor.forClass(Struct.class);
        verify(index).query(eq(3), any(), isNull(), isNull(), isNull(), isNull(), captured.capture(),
                eq(false), eq(false));
        var fields = captured.getValue().getFieldsMap();
        assertThat(fields.get("text").getStringValue()).isEqualTo("hello");
        assertThat(fields.get("number").getNumberValue()).isEqualTo(0.25);
        assertThat(fields.get("boolean").getBoolValue()).isTrue();
        assertThat(fields.get("null").getKindCase()).isEqualTo(Value.KindCase.NULL_VALUE);
        assertThat(fields.get("emptyObject").getKindCase()).isEqualTo(Value.KindCase.STRUCT_VALUE);
        assertThat(fields.get("emptyArray").getKindCase()).isEqualTo(Value.KindCase.LIST_VALUE);
    }

    @Test
    void unsupportedFilterValuesAreRejectedRatherThanStringified() {
        for (Object value : List.of(new Object(), Double.NaN, Double.POSITIVE_INFINITY, Map.of(1, "bad-key"))) {
            assertThat(catchThrowable(() -> traced.query(List.of(1.0f), 3, null, Map.of("bad", value))))
                    .isInstanceOf(IllegalArgumentException.class);
        }
        verifyNoInteractions(index);
    }

    @Test
    @SuppressWarnings("deprecation")
    void fetchKeepsTheTypedResponseAndTheLegacyMapContract() {
        List<String> ids = List.of("id");
        var response = FetchResponse.newBuilder().setNamespace("ns")
                .putVectors("id", Vector.newBuilder().setId("id").addValues(0.1f).build())
                .build();
        when(index.fetch(ids, "ns")).thenReturn(response);
        assertThat(traced.fetchResponse(ids, "ns")).isSameAs(response);
        Map<String, Object> legacy = traced.fetch(ids, "ns");
        assertThat(legacy).containsOnlyKeys("response");
        assertThat(legacy.get("response")).isSameAs(response);
        legacy.put("still-mutable", true);
        verify(index, times(2)).fetch(same(ids), eq("ns"));
        assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getAttributes().get(AttributeKey.longKey("pinecone.fetched_count"))).isEqualTo(1);
        });
    }

    @Test
    void writesPreserveArgumentsNativeResultsAndConvenienceContracts() {
        var vectors = List.of(new VectorWithUnsignedIndices("id", List.of(1.0f)));
        List<String> ids = List.of("id");
        var upsert = UpsertResponse.newBuilder().setUpsertedCount(7).build();
        var delete = DeleteResponse.getDefaultInstance();
        when(index.upsert(vectors, null)).thenReturn(upsert);
        when(index.deleteByIds(ids, null)).thenReturn(delete);
        assertThat(traced.upsertResponse(vectors, null)).isSameAs(upsert);
        assertThat(traced.upsert(vectors, null)).isEqualTo(1);
        assertThat(traced.deleteByIdsResponse(ids, null)).isSameAs(delete);
        traced.deleteByIds(ids, null);
        verify(index, times(2)).upsert(same(vectors), isNull());
        verify(index, times(2)).deleteByIds(same(ids), isNull());
        assertThat(exporter.getFinishedSpanItems()).hasSize(4);
        assertThat(traced.unwrap()).isSameAs(index);
    }

    @Test
    @SuppressWarnings("deprecation")
    void allOperationsPreserveSdkExceptionIdentityAndEndExactlyOneSpan() {
        RuntimeException failure = Status.UNAVAILABLE.withDescription("synthetic").asRuntimeException();
        when(index.query(anyInt(), any(), any(), any(), any(), any(), any(), anyBoolean(), anyBoolean()))
                .thenThrow(failure);
        when(index.fetch(any(), any())).thenThrow(failure);
        when(index.upsert(anyList(), any())).thenThrow(failure);
        when(index.deleteByIds(any(), any())).thenThrow(failure);
        List<ThrowingCallable> operations = List.of(
                () -> traced.query(List.of(1.0f), 3),
                () -> traced.query(List.of(1.0f), 3, "ns", Map.of()),
                () -> traced.fetchResponse(List.of("id"), null),
                () -> traced.fetch(List.of("id"), null),
                () -> traced.upsert(List.of(), null),
                () -> traced.upsertResponse(List.of(), null),
                () -> traced.deleteByIds(List.of("id"), null),
                () -> traced.deleteByIdsResponse(List.of("id"), null));
        operations.forEach(operation -> assertThat(catchThrowable(operation)).isSameAs(failure));
        assertThat(exporter.getFinishedSpanItems()).hasSize(operations.size()).allSatisfy(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getEvents()).hasSize(1);
            assertThat(span.hasEnded()).isTrue();
        });
    }

    @Test
    void filtersAreNotExportedAsTraceAttributes() {
        traced.query(List.of(1.0f), 3, null, Map.of("email", "sensitive-filter@example.invalid"));
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        assertThat(exporter.getFinishedSpanItems().get(0).getAttributes()
                .get(AttributeKey.stringKey("pinecone.filter"))).isNull();
    }
}
