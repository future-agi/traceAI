package ai.traceai.cohere;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.cohere.api.core.RequestOptions;
import com.cohere.api.resources.v2.V2Client;
import com.cohere.api.resources.v2.requests.V2ChatRequest;
import com.cohere.api.resources.v2.requests.V2ChatStreamRequest;
import com.cohere.api.resources.v2.requests.V2EmbedRequest;
import com.cohere.api.resources.v2.requests.V2RerankRequest;
import com.cohere.api.resources.v2.types.V2ChatResponse;
import com.cohere.api.resources.v2.types.V2ChatStreamResponse;
import com.cohere.api.resources.v2.types.V2RerankResponse;
import com.cohere.api.types.EmbedByTypeResponse;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.function.BiFunction;

/** Typed tracing for Cohere v2 chat, chat streaming, embed and rerank. */
public final class TracedCohereV2Client {
    private final V2Client client;
    private final FITracer tracer;

    /**
     * Wraps an SDK v2 client without changing its requests, options or responses.
     * @param client SDK v2 client
     * @param tracer tracer used for the supported operations
     */
    public TracedCohereV2Client(V2Client client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    public V2ChatResponse chat(V2ChatRequest request) {
        return chat(request, null);
    }

    public V2ChatResponse chat(V2ChatRequest request, RequestOptions options) {
        return call("Cohere V2 Chat", FISpanKind.LLM, (span, attributes) -> {
            attributes.model(request.getModel());
            request.getTemperature().ifPresent(value -> span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, value.doubleValue()));
            request.getMaxTokens().ifPresent(value -> span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, value.longValue()));
            attributes.chatInput(request);
            V2ChatResponse response = client.chat(request, options);
            span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, response.getId());
            attributes.finishReason(response.getFinishReason().toString(), null);
            response.getUsage().ifPresent(attributes::usage);
            attributes.chatOutput(response);
            return response;
        });
    }

    public EmbedByTypeResponse embed(V2EmbedRequest request) {
        return embed(request, null);
    }

    public EmbedByTypeResponse embed(V2EmbedRequest request, RequestOptions options) {
        return call("Cohere V2 Embed", FISpanKind.EMBEDDING, (span, attributes) -> {
            attributes.model(request.getModel());
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, request.getModel());
            span.setAttribute("cohere.input_type", request.getInputType().toString());
            request.getTexts().ifPresent(texts -> tracer.setInputValue(span, tracer.toJson(texts)));
            attributes.rawInput(request);
            EmbedByTypeResponse response = client.embed(request, options);
            var vectors = response.getEmbeddings();
            // Prefer a non-packed encoding when multiple formats were requested.
            List<? extends List<? extends Number>> values = vectors.getFloat().orElse(null);
            if (values == null) values = vectors.getInt8().orElse(null);
            if (values == null) values = vectors.getUint8().orElse(null);
            if (values != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) values.size());
                if (!values.isEmpty()) span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) values.get(0).size());
            } else {
                // Binary vectors are packed; their element count is not the dimension.
                vectors.getBinary().ifPresent(value -> span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) value.size()));
                vectors.getUbinary().ifPresent(value -> span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) value.size()));
                vectors.getBase64().ifPresent(value -> span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) value.size()));
            }
            response.getMeta().ifPresent(attributes::meta);
            attributes.rawOutput(response);
            return response;
        });
    }

    public V2RerankResponse rerank(V2RerankRequest request) {
        return rerank(request, null);
    }

    public V2RerankResponse rerank(V2RerankRequest request, RequestOptions options) {
        return call("Cohere V2 Rerank", FISpanKind.RERANKER, (span, attributes) -> {
            attributes.model(request.getModel());
            tracer.setInputValue(span, request.getQuery());
            if (!tracer.getConfig().isHideInputs()) span.setAttribute(SemanticConventions.GEN_AI_RERANKER_QUERY, request.getQuery());
            span.setAttribute(SemanticConventions.GEN_AI_RERANKER_INPUT_DOCUMENTS, (long) request.getDocuments().size());
            request.getTopN().ifPresent(value -> span.setAttribute(SemanticConventions.GEN_AI_RERANKER_TOP_N, value.longValue()));
            attributes.rawInput(request);
            V2RerankResponse response = client.rerank(request, options);
            span.setAttribute(SemanticConventions.GEN_AI_RERANKER_OUTPUT_DOCUMENTS, (long) response.getResults().size());
            if (!response.getResults().isEmpty()) {
                var first = response.getResults().get(0);
                span.setAttribute("cohere.rerank.top_score", (double) first.getRelevanceScore());
                span.setAttribute("cohere.rerank.top_index", (long) first.getIndex());
            }
            response.getMeta().ifPresent(attributes::meta);
            attributes.rawOutput(response);
            return response;
        });
    }

    /** Use try-with-resources so early termination also closes the SDK stream and span. */
    public TracedCohereStream<V2ChatStreamResponse> chatStream(V2ChatStreamRequest request) {
        return chatStream(request, null);
    }

    public TracedCohereStream<V2ChatStreamResponse> chatStream(V2ChatStreamRequest request, RequestOptions options) {
        Span span = tracer.startSpan("Cohere V2 Chat Stream", FISpanKind.LLM);
        CohereAttributes attributes = new CohereAttributes(tracer, span);
        try (Scope ignored = span.makeCurrent()) {
            attributes.model(request.getModel());
            request.getTemperature().ifPresent(value -> span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, value.doubleValue()));
            request.getMaxTokens().ifPresent(value -> span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, value.longValue()));
            attributes.chatInput(request);
            return new TracedCohereStream<>(client.chatStream(request, options), tracer, span,
                attributes::v2Event, attributes::streamOutput);
        } catch (RuntimeException | Error error) {
            tracer.setError(span, error);
            span.end();
            throw error;
        }
    }

    private <T> T call(String name, FISpanKind kind, BiFunction<Span, CohereAttributes, T> operation) {
        Span span = tracer.startSpan(name, kind);
        try (Scope ignored = span.makeCurrent()) {
            CohereAttributes attributes = new CohereAttributes(tracer, span);
            T response = operation.apply(span, attributes);
            if (!attributes.hasError()) span.setStatus(StatusCode.OK);
            return response;
        } catch (RuntimeException | Error error) {
            tracer.setError(span, error);
            throw error;
        } finally {
            span.end();
        }
    }
}
