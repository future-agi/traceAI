package local.th7477;

import ai.traceai.FITracer;
import ai.traceai.spring.TracedEmbeddingModel;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.springframework.ai.document.Document;

/** P-only: getEmbeddingContent exists in Spring AI 1.1.8, not 1.1.0. Must not open a span. */
public final class PrimaryConsumer {
    public static void main(String[] args) {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        FITracer tracer = new FITracer(provider.get("th7477-primary"));
        TracedEmbeddingModel model = new TracedEmbeddingModel(new SpanTestSupport.FixedEmbeddingModel(), tracer, "synthetic");
        String content = model.getEmbeddingContent(new Document("doc-text"));
        if (!"doc-text".equals(content)) throw new AssertionError("getEmbeddingContent returned " + content);
        if (!exporter.getFinishedSpanItems().isEmpty()) throw new AssertionError("getEmbeddingContent opened a span");
        System.out.println("PRIMARY_ONLY_PASS getEmbeddingContent=" + content);
        provider.close();
    }
}
