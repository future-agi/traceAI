package ai.traceai.chromadb;

import static org.assertj.core.api.Assertions.assertThat;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import tech.amikos.chromadb.Collection;
import tech.amikos.chromadb.model.QueryEmbedding.IncludeEnum;

/**
 * The query text of a ChromaDB query is the only content key the module writes (input.value, through
 * FITracer.setInputValue). It must not be exported under hideInputs or hideInputMessages.
 * The ChromaDB Collection is a Mockito mock: no server, no network.
 */
class TracedChromaContentMaskTest {
    private static final String SENTINEL = "SENTINEL-CHROMA-QUERY-TEXT-7f3a9c";

    private static String exportedText(TraceConfig config) throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        List<SpanData> spans;
        try (SdkTracerProvider provider = SdkTracerProvider.builder()
                .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
            Collection collection = new Collection(null, "docs", null) {
                @Override
                public QueryResponse query(List<String> queryTexts, Integer nResults,
                                           Map<String, Object> where, Map<String, Object> whereDocument,
                                           List<IncludeEnum> include) {
                    return null;
                }
            };
            TracedChromaCollection traced = new TracedChromaCollection(
                    collection, new FITracer(provider.get("chroma-mask"), config), "docs");
            traced.query(List.of(SENTINEL, SENTINEL + "-second"), 3, Map.of("k", "v"), null,
                    List.of(IncludeEnum.DOCUMENTS));
            spans = exporter.getFinishedSpanItems();
        }
        assertThat(spans).hasSize(1);
        StringBuilder all = new StringBuilder(spans.get(0).getAttributes().toString());
        for (EventData event : spans.get(0).getEvents()) {
            all.append(event.getName()).append(event.getAttributes());
        }
        return all.toString();
    }

    @Test
    void queryTextIsExportedWhenNoFlagIsSet() throws Exception {
        assertThat(exportedText(TraceConfig.builder().build())).contains(SENTINEL);
    }

    @Test
    void queryTextIsNotExportedUnderHideInputs() throws Exception {
        assertThat(exportedText(TraceConfig.builder().hideInputs(true).build())).doesNotContain(SENTINEL);
    }

    @Test
    void queryTextIsNotExportedUnderHideInputMessages() throws Exception {
        assertThat(exportedText(TraceConfig.builder().hideInputMessages(true).build()))
                .doesNotContain(SENTINEL);
    }

    @Test
    void queryTextIsNotExportedUnderAllFlags() throws Exception {
        assertThat(exportedText(TraceConfig.builder().hideInputs(true).hideOutputs(true)
                .hideInputMessages(true).hideOutputMessages(true).hideErrorDetails(true).build()))
                .doesNotContain(SENTINEL);
    }
}
