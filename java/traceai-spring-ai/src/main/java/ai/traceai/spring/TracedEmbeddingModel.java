package ai.traceai.spring;

import ai.traceai.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import org.springframework.ai.document.Document;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.embedding.EmbeddingRequest;
import org.springframework.ai.embedding.EmbeddingResponse;

import java.util.List;

/**
 * Traced wrapper for Spring AI EmbeddingModel.
 * Implements the EmbeddingModel interface to provide automatic tracing.
 *
 * <p>Usage:</p>
 * <pre>
 * EmbeddingModel model = new OpenAiEmbeddingModel(api);
 * EmbeddingModel tracedModel = new TracedEmbeddingModel(model, "openai");
 *
 * EmbeddingResponse response = tracedModel.embed(List.of("Hello world"));
 * </pre>
 */
public class TracedEmbeddingModel implements EmbeddingModel {

    private final EmbeddingModel delegate;
    private final FITracer tracer;
    private final String provider;

    /**
     * Creates a new traced embedding model.
     *
     * @param delegate the underlying model to wrap
     * @param tracer   the FITracer for instrumentation
     * @param provider the provider name (e.g., "openai", "cohere")
     */
    public TracedEmbeddingModel(EmbeddingModel delegate, FITracer tracer, String provider) {
        this.delegate = delegate;
        this.tracer = tracer;
        this.provider = provider;
    }

    /**
     * Creates a new traced embedding model using the global TraceAI tracer.
     *
     * @param delegate the underlying model to wrap
     * @param provider the provider name
     */
    public TracedEmbeddingModel(EmbeddingModel delegate, String provider) {
        this(delegate, TraceAI.getTracer(), provider);
    }

    @Override
    public EmbeddingResponse call(EmbeddingRequest request) {
        Span span = tracer.startSpan("Spring AI Embedding", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "spring-ai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);

            // Capture input
            if (request != null && request.getInstructions() != null) {
                List<String> instructions = request.getInstructions();
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) instructions.size());

                // Capture first few texts
                StringBuilder inputBuilder = new StringBuilder();
                int maxTexts = Math.min(instructions.size(), 5);
                for (int i = 0; i < maxTexts; i++) {
                    if (i > 0) inputBuilder.append("\n---\n");
                    inputBuilder.append(instructions.get(i));
                }
                if (instructions.size() > 5) {
                    inputBuilder.append("\n... and ").append(instructions.size() - 5).append(" more");
                }
                tracer.setInputValue(span, inputBuilder.toString());
            }

            // Capture model if available
            if (request != null && request.getOptions() != null && request.getOptions().getModel() != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, request.getOptions().getModel());
            }

            // Execute embedding
            EmbeddingResponse response = delegate.call(request);

            // Capture output
            if (response != null && response.getResults() != null && !response.getResults().isEmpty()) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT,
                    (long) response.getResults().size());

                // Get dimensions from first embedding
                var firstEmbedding = response.getResults().get(0);
                if (firstEmbedding != null && firstEmbedding.getOutput() != null) {
                    span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                        (long) firstEmbedding.getOutput().length);
                }
            }

            // Token usage
            if (response != null && response.getMetadata() != null && response.getMetadata().getUsage() != null) {
                var usage = response.getMetadata().getUsage();
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, usage.getPromptTokens());
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, usage.getTotalTokens());
            }

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    @Override
    public float[] embed(Document document) {
        return delegate.embed(document);
    }

    @Override
    public float[] embed(String text) {
        return delegate.embed(text);
    }

    @Override
    public List<float[]> embed(List<String> texts) {
        return delegate.embed(texts);
    }

    @Override
    public EmbeddingResponse embedForResponse(List<String> texts) {
        return delegate.embedForResponse(texts);
    }

    @Override
    public int dimensions() {
        return delegate.dimensions();
    }

    /**
     * Gets the underlying model.
     *
     * @return the wrapped EmbeddingModel
     */
    public EmbeddingModel unwrap() {
        return delegate;
    }
}
