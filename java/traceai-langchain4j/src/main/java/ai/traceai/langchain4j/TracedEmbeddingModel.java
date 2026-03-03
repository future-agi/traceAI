package ai.traceai.langchain4j;

import ai.traceai.*;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.output.Response;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;

/**
 * Traced wrapper for LangChain4j EmbeddingModel.
 * Implements the EmbeddingModel interface to provide automatic tracing.
 *
 * <p>Usage:</p>
 * <pre>
 * EmbeddingModel model = OpenAiEmbeddingModel.builder()
 *     .apiKey("...")
 *     .modelName("text-embedding-3-small")
 *     .build();
 *
 * EmbeddingModel tracedModel = new TracedEmbeddingModel(model, "openai");
 *
 * Response&lt;Embedding&gt; response = tracedModel.embed("Hello world");
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
    public Response<List<Embedding>> embedAll(List<TextSegment> textSegments) {
        Span span = tracer.startSpan("LangChain4j Embedding", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "langchain4j");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);

            // Capture input
            if (textSegments != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) textSegments.size());

                // Capture first few texts for context
                StringBuilder inputBuilder = new StringBuilder();
                int maxTexts = Math.min(textSegments.size(), 5);
                for (int i = 0; i < maxTexts; i++) {
                    if (i > 0) inputBuilder.append("\n---\n");
                    inputBuilder.append(textSegments.get(i).text());
                }
                if (textSegments.size() > 5) {
                    inputBuilder.append("\n... and ").append(textSegments.size() - 5).append(" more");
                }
                tracer.setInputValue(span, inputBuilder.toString());
            }

            // Execute embedding
            Response<List<Embedding>> response = delegate.embedAll(textSegments);

            // Capture output
            if (response.content() != null && !response.content().isEmpty()) {
                List<Embedding> embeddings = response.content();
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) embeddings.size());
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embeddings.get(0).dimension());
            }

            // Token usage
            if (response.tokenUsage() != null) {
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
                    (long) response.tokenUsage().inputTokenCount());
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
                    (long) response.tokenUsage().totalTokenCount());
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

    /**
     * Gets the underlying model.
     *
     * @return the wrapped EmbeddingModel
     */
    public EmbeddingModel unwrap() {
        return delegate;
    }
}
