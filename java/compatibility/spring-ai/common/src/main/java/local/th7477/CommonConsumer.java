package local.th7477;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.spring.TracedChatModel;
import ai.traceai.spring.TracedEmbeddingModel;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.embedding.EmbeddingRequest;

import java.security.CodeSource;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.List;

/** Same compiled classes, same candidate JARs, run on both Spring AI cells. Asserts common API only. */
public final class CommonConsumer {
    public static void main(String[] args) throws Exception {
        String cell = args.length > 0 ? args[0] : "unknown";
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        FITracer tracer = new FITracer(provider.get("th7477-matrix"));

        ChatResponse expected = SpanTestSupport.response("matrix-answer", SpanTestSupport.usage(4, 2, 6), "matrix-model");
        TracedChatModel traced = new TracedChatModel(SpanTestSupport.syncModel(p -> expected), tracer, "synthetic");
        ChatResponse actual = traced.call(new Prompt(List.of(new UserMessage("matrix input " + cell))));
        if (actual != expected) throw new AssertionError("call identity lost on " + cell);

        SpanData span = exporter.getFinishedSpanItems().get(exporter.getFinishedSpanItems().size() - 1);
        eq(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE)), "matrix-answer", "output");
        eq(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_PROMPT)), 4L, "prompt tokens");
        eq(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)), 2L, "completion tokens");
        eq(span.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL)), 6L, "total tokens");

        traced.stream(new Prompt("stream " + cell)).blockLast();
        long streamSpans = exporter.getFinishedSpanItems().stream().filter(s -> s.getName().contains("Stream")).count();
        if (streamSpans != 1) throw new AssertionError("expected one stream span, got " + streamSpans);

        TracedEmbeddingModel tracedEmbeddings = new TracedEmbeddingModel(new SpanTestSupport.FixedEmbeddingModel(), tracer, "synthetic");
        List<float[]> vectors = tracedEmbeddings.embed(List.of("a", "b"));
        if (vectors.size() != 2 || vectors.get(0).length != 3) throw new AssertionError("batch identity lost");
        tracedEmbeddings.call(new EmbeddingRequest(List.of("a"), null));

        CodeSource source = TracedChatModel.class.getProtectionDomain().getCodeSource();
        String digest = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(source.getLocation().openStream().readAllBytes()));
        System.out.println("CELL=" + cell + " SHA256=" + digest + " SPANS=" + exporter.getFinishedSpanItems().size() + " COMMON_CONSUMER_PASS");
        provider.close();
    }

    private static void eq(Object actual, Object expected, String what) {
        if (actual == null || !actual.equals(expected)) throw new AssertionError(what + " expected " + expected + " but was " + actual);
    }
}
