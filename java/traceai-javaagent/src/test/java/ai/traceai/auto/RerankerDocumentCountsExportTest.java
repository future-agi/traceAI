package ai.traceai.auto;

import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.util.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import static org.junit.jupiter.api.Assertions.*;

/**
 * A wrapper span's reranker document counts (Cohere rerank writes gen_ai.reranker.input_documents and
 * output_documents as whole numbers) are exported under every hide switch; the documents' text is not.
 */
class RerankerDocumentCountsExportTest {
    static final List<String> COUNT_KEYS = List.of(
        "gen_ai.reranker.input_documents", "gen_ai.reranker.output_documents", "reranker.input_documents", "reranker.output_documents");

    @ParameterizedTest
    @ValueSource(strings = {"hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages", "hide_input_text",
        "hide_output_text", "hide_input_images", "hide_llm_invocation_parameters", "hide_error_details"})
    void documentCountsAreExportedUnderEveryHideSwitch(String flag) {
        var memory = InMemorySpanExporter.create();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var span = provider.get("existing-wrapper").spanBuilder("Cohere Rerank").startSpan();
            for (int i = 0; i < COUNT_KEYS.size(); i++) span.setAttribute(COUNT_KEYS.get(i), 7310L + i);
            span.setAttribute("gen_ai.reranker.input_documents.0.document.content", "PRIVATE_DOCUMENT");
            span.end();
            SpanData data = memory.getFinishedSpanItems().get(0);
            var config = AutoConfig.resolve(Map.of(flag, true), Map.of());
            String json = WireCore.dump(new OtlpJsonExporter(config).serialize(data));
            for (int i = 0; i < COUNT_KEYS.size(); i++) {
                assertTrue(json.contains(COUNT_KEYS.get(i)) && json.contains(String.valueOf(7310L + i)), flag + " withheld " + COUNT_KEYS.get(i));
            }
            // The documents themselves are input content: hidden under hide_inputs, as before.
            if (flag.equals("hide_inputs")) assertFalse(json.contains("PRIVATE_DOCUMENT"), flag + " exported the document text");
        }
    }
}
