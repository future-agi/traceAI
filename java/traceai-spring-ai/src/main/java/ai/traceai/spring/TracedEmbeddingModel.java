package ai.traceai.spring;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import org.springframework.ai.document.Document;
import org.springframework.ai.embedding.BatchingStrategy;
import org.springframework.ai.embedding.Embedding;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.embedding.EmbeddingOptions;
import org.springframework.ai.embedding.EmbeddingRequest;
import org.springframework.ai.embedding.EmbeddingResponse;
import org.springframework.ai.embedding.EmbeddingResponseMetadata;

import java.util.List;
import java.util.Objects;
import java.util.function.Supplier;

/**
 * Traced wrapper for a Spring AI 1.1.x {@link EmbeddingModel}.
 *
 * <p>Every overload delegates directly to the same overload of the wrapped model with the original
 * arguments, options and batching strategy, under exactly one span. Return values are the delegate's
 * exact objects. Token usage is recorded only when the provider response carries it; bare vector
 * overloads never get inferred usage.</p>
 *
 * <p>Usage:</p>
 * <pre>
 * EmbeddingModel model = new OpenAiEmbeddingModel(api);
 * EmbeddingModel tracedModel = new TracedEmbeddingModel(model, tracer, "openai");
 *
 * List&lt;float[]&gt; vectors = tracedModel.embed(List.of("Hello world"));
 * </pre>
 */
public class TracedEmbeddingModel implements EmbeddingModel {

    static final String SPAN_NAME = "Spring AI Embedding";
    static final int INPUT_PREVIEW_TEXTS = 5;

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
        this.delegate = Objects.requireNonNull(delegate, "delegate EmbeddingModel must not be null");
        this.tracer = Objects.requireNonNull(tracer, "FITracer must not be null");
        this.provider = provider;
    }

    /**
     * Creates a new traced embedding model using the global TraceAI tracer.
     *
     * @param delegate the underlying model to wrap
     * @param provider the provider name
     * @throws IllegalStateException if {@link TraceAI#init} has not been called
     */
    public TracedEmbeddingModel(EmbeddingModel delegate, String provider) {
        this(delegate, TraceAI.getTracer(), provider);
    }

    @Override
    public EmbeddingResponse call(EmbeddingRequest request) {
        return traced(span -> {
            if (request != null) {
                captureInputTexts(span, request.getInstructions());
                EmbeddingOptions options = request.getOptions();
                if (options != null && options.getModel() != null) {
                    span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, options.getModel());
                    span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, options.getModel());
                }
            }
        }, () -> delegate.call(request), (span, response) -> {
            boolean requestModelKnown = request != null && request.getOptions() != null && request.getOptions().getModel() != null;
            captureResponse(span, response, requestModelKnown);
        });
    }

    @Override
    public float[] embed(Document document) {
        return traced(span -> {
            if (document != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, 1L);
            }
        }, () -> delegate.embed(document), (span, vector) -> captureVector(span, vector));
    }

    @Override
    public float[] embed(String text) {
        return traced(span -> captureInputTexts(span, text == null ? null : List.of(text)),
            () -> delegate.embed(text), (span, vector) -> captureVector(span, vector));
    }

    /**
     * {@inheritDoc}
     *
     * @return one vector per input text, in input order (the delegate's exact list)
     */
    @Override
    public List<float[]> embed(List<String> texts) {
        return traced(span -> captureInputTexts(span, texts), () -> delegate.embed(texts), (span, vectors) -> captureVectors(span, vectors));
    }

    @Override
    public List<float[]> embed(List<Document> documents, EmbeddingOptions options, BatchingStrategy batchingStrategy) {
        return traced(span -> {
            if (documents != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) documents.size());
            }
            if (options != null && options.getModel() != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, options.getModel());
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, options.getModel());
            }
        }, () -> delegate.embed(documents, options, batchingStrategy), (span, vectors) -> captureVectors(span, vectors));
    }

    @Override
    public EmbeddingResponse embedForResponse(List<String> texts) {
        return traced(span -> captureInputTexts(span, texts), () -> delegate.embedForResponse(texts),
            (span, response) -> captureResponse(span, response, false));
    }

    /** Delegates directly; no probe embedding is issued by the wrapper. */
    @Override
    public int dimensions() {
        return delegate.dimensions();
    }

    /**
     * Delegates directly without a model-operation span. This accessor exists in Spring AI 1.1.8 and later
     * only; an application on an older 1.1.x line simply never calls it.
     */
    @Override
    public String getEmbeddingContent(Document document) {
        return delegate.getEmbeddingContent(document);
    }

    /**
     * Gets the underlying model.
     *
     * @return the wrapped EmbeddingModel
     */
    public EmbeddingModel unwrap() {
        return delegate;
    }

    // ---------------------------------------------------------------------------------------------

    private interface SpanConsumer {
        void accept(Span span);
    }

    private interface SpanResultConsumer<T> {
        void accept(Span span, T result);
    }

    private <T> T traced(SpanConsumer before, Supplier<T> operation, SpanResultConsumer<T> after) {
        Span span = SpringAITracingContext.startSpan(tracer, SPAN_NAME, FISpanKind.EMBEDDING, SpringAITracingContext.capture());
        boolean completed = false;
        boolean errorRecorded = false;
        try (Scope ignored = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "spring-ai");
            if (provider != null) {
                span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            }
            before.accept(span);
            T result = operation.get();
            after.accept(span, result);
            span.setStatus(StatusCode.OK);
            completed = true;
            return result;
        } catch (RuntimeException e) {
            errorRecorded = true;
            tracer.setError(span, e);
            throw e;
        } finally {
            if (!completed && !errorRecorded) {
                span.setStatus(StatusCode.ERROR, "abnormal termination");
            }
            span.end();
        }
    }

    private void captureInputTexts(Span span, List<String> texts) {
        if (texts == null) {
            return;
        }
        span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) texts.size());
        StringBuilder preview = new StringBuilder();
        int shown = Math.min(texts.size(), INPUT_PREVIEW_TEXTS);
        for (int i = 0; i < shown; i++) {
            if (i > 0) {
                preview.append("\n---\n");
            }
            String text = texts.get(i);
            if (text != null) {
                preview.append(text);
            }
        }
        if (texts.size() > INPUT_PREVIEW_TEXTS) {
            preview.append("\n... and ").append(texts.size() - INPUT_PREVIEW_TEXTS).append(" more");
        }
        tracer.setInputValue(span, preview.toString());
    }

    private void captureResponse(Span span, EmbeddingResponse response, boolean requestModelKnown) {
        if (response == null) {
            return;
        }
        EmbeddingResponseMetadata metadata = response.getMetadata();
        if (metadata != null) {
            String responseModel = metadata.getModel();
            if (responseModel != null && !responseModel.isEmpty()) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, responseModel);
                if (!requestModelKnown) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, responseModel);
                    span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, responseModel);
                }
            }
        }
        List<Embedding> results = response.getResults();
        if (results != null && !results.isEmpty()) {
            span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) results.size());
            Embedding first = results.get(0);
            if (first != null && first.getOutput() != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) first.getOutput().length);
            }
        }
        if (metadata != null) {
            applyUsage(span, metadata.getUsage());
        }
    }

    private static void captureVector(Span span, float[] vector) {
        if (vector != null) {
            span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, 1L);
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) vector.length);
        }
    }

    private static void captureVectors(Span span, List<float[]> vectors) {
        if (vectors == null) {
            return;
        }
        span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) vectors.size());
        if (!vectors.isEmpty() && vectors.get(0) != null) {
            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) vectors.get(0).length);
        }
    }

    /** Prompt and total counts are read once and set only when present; zero is preserved. */
    private static void applyUsage(Span span, org.springframework.ai.chat.metadata.Usage usage) {
        if (usage == null) {
            return;
        }
        Integer prompt = usage.getPromptTokens();
        Integer total = usage.getTotalTokens();
        Object nativeUsage = usage.getNativeUsage();
        // EmptyUsage (no usage reported) returns zeros plus an empty native map; that is absence, not zero.
        if (nativeUsage instanceof java.util.Map && ((java.util.Map<?, ?>) nativeUsage).isEmpty()
            && Integer.valueOf(0).equals(prompt) && Integer.valueOf(0).equals(usage.getCompletionTokens())
            && Integer.valueOf(0).equals(total)) {
            return;
        }
        if (prompt != null) {
            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, prompt.longValue());
        }
        if (total != null) {
            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, total.longValue());
        }
    }
}
