package ai.traceai.semantickernel;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import com.microsoft.semantickernel.services.textembedding.Embedding;
import com.microsoft.semantickernel.services.textembedding.TextEmbeddingGenerationService;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import reactor.core.publisher.Mono;

import java.util.List;

/**
 * Instrumentation wrapper for Semantic Kernel TextEmbeddingGenerationService.
 * Wraps the TextEmbeddingGenerationService to provide automatic tracing of embedding generation.
 *
 * <p>Usage:</p>
 * <pre>
 * TextEmbeddingGenerationService embeddingService = OpenAITextEmbeddingGeneration.builder()
 *     .withModelId("text-embedding-ada-002")
 *     .withOpenAIAsyncClient(client)
 *     .build();
 *
 * TracedTextEmbeddingGenerationService tracedService = new TracedTextEmbeddingGenerationService(
 *     embeddingService, "text-embedding-ada-002", "openai"
 * );
 *
 * List&lt;Embedding&gt; embeddings = tracedService.generateEmbeddingsAsync(
 *     List.of("Hello", "World")
 * ).block();
 * </pre>
 */
public class TracedTextEmbeddingGenerationService implements TextEmbeddingGenerationService {

    private static final String LLM_SYSTEM = "semantic-kernel";

    private final TextEmbeddingGenerationService delegate;
    private final String modelName;
    private final String provider;
    private final FITracer tracer;

    /**
     * Creates a new traced TextEmbeddingGenerationService with the given service, model name, provider, and tracer.
     *
     * @param delegate the TextEmbeddingGenerationService to wrap
     * @param modelName the model name (e.g., "text-embedding-ada-002", "text-embedding-3-small")
     * @param provider the provider name (e.g., "openai", "azure")
     * @param tracer the FITracer for instrumentation
     */
    public TracedTextEmbeddingGenerationService(TextEmbeddingGenerationService delegate, String modelName, String provider, FITracer tracer) {
        this.delegate = delegate;
        this.modelName = modelName;
        this.provider = provider;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced TextEmbeddingGenerationService with the given service, model name, and provider.
     * Uses the global TraceAI tracer.
     *
     * @param delegate the TextEmbeddingGenerationService to wrap
     * @param modelName the model name
     * @param provider the provider name
     */
    public TracedTextEmbeddingGenerationService(TextEmbeddingGenerationService delegate, String modelName, String provider) {
        this(delegate, modelName, provider, TraceAI.getTracer());
    }

    /**
     * Creates a new traced TextEmbeddingGenerationService with the given service and model name.
     * Provider defaults to "openai".
     *
     * @param delegate the TextEmbeddingGenerationService to wrap
     * @param modelName the model name
     */
    public TracedTextEmbeddingGenerationService(TextEmbeddingGenerationService delegate, String modelName) {
        this(delegate, modelName, "openai");
    }

    @Override
    public Mono<List<Embedding>> generateEmbeddingsAsync(List<String> data) {
        Span span = tracer.startSpan("Semantic Kernel Embedding Generation", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, modelName);

            // Capture input
            captureInput(span, data);

            // Execute the request
            return delegate.generateEmbeddingsAsync(data)
                .doOnSuccess(embeddings -> {
                    // Capture output
                    captureOutput(span, embeddings);
                    span.setStatus(StatusCode.OK);
                })
                .doOnError(error -> {
                    tracer.setError(span, error);
                })
                .doFinally(signalType -> {
                    span.end();
                });
        }
    }

    @Override
    public Mono<Embedding> generateEmbeddingAsync(String data) {
        Span span = tracer.startSpan("Semantic Kernel Single Embedding", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, LLM_SYSTEM);
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, modelName);

            if (data != null) {
                tracer.setInputValue(span, data);
            }

            return delegate.generateEmbeddingAsync(data)
                .doOnSuccess(embedding -> {
                    if (embedding != null && embedding.getVector() != null) {
                        span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embedding.getVector().size());
                    }
                    span.setStatus(StatusCode.OK);
                })
                .doOnError(error -> {
                    tracer.setError(span, error);
                })
                .doFinally(signalType -> {
                    span.end();
                });
        }
    }

    @Override
    public String getModelId() {
        return delegate.getModelId();
    }

    @Override
    public String getServiceId() {
        return delegate.getServiceId();
    }

    /**
     * Gets the underlying TextEmbeddingGenerationService.
     *
     * @return the wrapped TextEmbeddingGenerationService
     */
    public TextEmbeddingGenerationService unwrap() {
        return delegate;
    }

    /**
     * Gets the tracer being used.
     *
     * @return the FITracer
     */
    public FITracer getTracer() {
        return tracer;
    }

    private void captureInput(Span span, List<String> data) {
        if (data == null || data.isEmpty()) {
            return;
        }

        // Set the number of texts being embedded
        span.setAttribute("embedding.input_count", (long) data.size());

        // Capture input texts
        StringBuilder inputBuilder = new StringBuilder();
        for (int i = 0; i < data.size(); i++) {
            String text = data.get(i);
            if (i > 0) {
                inputBuilder.append("\n---\n");
            }
            inputBuilder.append(text);

            // Set individual text attributes for first few texts
            if (i < 5) {
                span.setAttribute(SemanticConventions.EMBEDDING_INPUT_TEXT + "." + i, truncateIfNeeded(text));
            }
        }

        tracer.setInputValue(span, inputBuilder.toString());
        tracer.setRawInput(span, data);
    }

    private void captureOutput(Span span, List<Embedding> embeddings) {
        if (embeddings == null || embeddings.isEmpty()) {
            return;
        }

        // Set vector count
        span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) embeddings.size());

        // Get dimensions from first embedding
        Embedding firstEmbedding = embeddings.get(0);
        if (firstEmbedding != null && firstEmbedding.getVector() != null) {
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) firstEmbedding.getVector().size());
        }

        // Set output summary
        String outputSummary = String.format("Generated %d embeddings", embeddings.size());
        if (firstEmbedding != null && firstEmbedding.getVector() != null) {
            outputSummary += String.format(" with %d dimensions each", firstEmbedding.getVector().size());
        }
        tracer.setOutputValue(span, outputSummary);

        // Note: We don't capture raw output for embeddings as they can be very large
        // Just capture metadata
        tracer.setRawOutput(span, new EmbeddingOutputSummary(embeddings.size(),
            firstEmbedding != null && firstEmbedding.getVector() != null ? firstEmbedding.getVector().size() : 0));
    }

    private String truncateIfNeeded(String value) {
        int maxLength = 1000;
        if (value != null && value.length() > maxLength) {
            return value.substring(0, maxLength - 13) + "...[truncated]";
        }
        return value;
    }

    /**
     * Internal class to summarize embedding output without storing full vectors.
     */
    private static class EmbeddingOutputSummary {
        final int vectorCount;
        final int dimensions;

        EmbeddingOutputSummary(int vectorCount, int dimensions) {
            this.vectorCount = vectorCount;
            this.dimensions = dimensions;
        }
    }
}
