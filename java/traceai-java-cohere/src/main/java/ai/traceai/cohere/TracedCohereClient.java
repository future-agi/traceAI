package ai.traceai.cohere;

import ai.traceai.*;
import com.cohere.api.Cohere;
import com.cohere.api.requests.*;
import com.cohere.api.types.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Instrumentation wrapper for Cohere Java client.
 * Wraps the Cohere client to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * Cohere cohere = Cohere.builder().token("api-key").build();
 * TracedCohereClient traced = new TracedCohereClient(cohere);
 *
 * NonStreamedChatResponse response = traced.chat(ChatRequest.builder()
 *     .message("Hello!")
 *     .build());
 * </pre>
 */
public class TracedCohereClient {

    private final Cohere client;
    private final FITracer tracer;

    /**
     * Creates a new traced Cohere client with the given client and tracer.
     *
     * @param client the Cohere client to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedCohereClient(Cohere client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Cohere client using the global TraceAI tracer.
     *
     * @param client the Cohere client to wrap
     */
    public TracedCohereClient(Cohere client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Sends a chat request with tracing.
     *
     * @param request the chat request
     * @return the chat response
     */
    public NonStreamedChatResponse chat(ChatRequest request) {
        Span span = tracer.startSpan("Cohere Chat", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "cohere");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "cohere");

            // Set model if specified
            request.getModel().ifPresent(model -> {
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, model);
            });

            // Set request parameters
            request.getTemperature().ifPresent(temp ->
                span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, temp.doubleValue()));
            request.getMaxTokens().ifPresent(maxTokens ->
                span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, maxTokens.longValue()));

            // Capture input message
            tracer.setInputValue(span, request.getMessage());
            List<Map<String, String>> inputMessages = new ArrayList<>();
            inputMessages.add(FITracer.message("user", request.getMessage()));

            // Capture chat history if present
            request.getChatHistory().ifPresent(history -> {
                for (Message msg : history) {
                    String role = extractMessageRole(msg);
                    String content = extractMessageContent(msg);
                    inputMessages.add(FITracer.message(role, content));
                }
            });
            tracer.setInputMessages(span, inputMessages);

            // Capture preamble/system prompt if present
            request.getPreamble().ifPresent(preamble ->
                span.setAttribute("cohere.preamble", preamble));

            tracer.setRawInput(span, request);

            // Execute request
            NonStreamedChatResponse response = client.chat(request);

            // Capture output
            if (response.getText() != null) {
                tracer.setOutputValue(span, response.getText());
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", response.getText())));
            }

            // Capture finish reason
            response.getFinishReason().ifPresent(reason ->
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, reason.toString()));

            // Capture response ID
            response.getGenerationId().ifPresent(genId ->
                span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, genId));

            // Token usage
            response.getMeta().ifPresent(meta ->
                meta.getTokens().ifPresent(tokens -> {
                    double inputTokens = tokens.getInputTokens().orElse(0.0);
                    double outputTokens = tokens.getOutputTokens().orElse(0.0);
                    tracer.setTokenCounts(span, (int) inputTokens, (int) outputTokens,
                        (int) (inputTokens + outputTokens));
                }));

            // Capture tool calls if present
            response.getToolCalls().ifPresent(toolCalls -> {
                for (int i = 0; i < toolCalls.size(); i++) {
                    ToolCall toolCall = toolCalls.get(i);
                    span.setAttribute("llm.tool_calls." + i + ".name", toolCall.getName());
                    span.setAttribute("llm.tool_calls." + i + ".parameters",
                        tracer.toJson(toolCall.getParameters()));
                }
            });

            tracer.setRawOutput(span, response);
            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Cohere chat failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Creates embeddings with tracing.
     *
     * @param request the embed request
     * @return the embed response
     */
    public EmbedResponse embed(EmbedRequest request) {
        Span span = tracer.startSpan("Cohere Embed", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "cohere");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "cohere");

            // Set model
            request.getModel().ifPresent(model ->
                span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, model));

            // Capture input texts
            request.getTexts().ifPresent(texts -> {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) texts.size());

                StringBuilder inputBuilder = new StringBuilder();
                int maxTexts = Math.min(texts.size(), 5);
                for (int i = 0; i < maxTexts; i++) {
                    if (i > 0) inputBuilder.append("\n---\n");
                    inputBuilder.append(texts.get(i));
                }
                if (texts.size() > 5) {
                    inputBuilder.append("\n... and ").append(texts.size() - 5).append(" more");
                }
                tracer.setInputValue(span, inputBuilder.toString());
            });

            // Capture embedding type
            request.getInputType().ifPresent(inputType ->
                span.setAttribute("cohere.input_type", inputType.toString()));

            tracer.setRawInput(span, request);

            // Execute request
            EmbedResponse response = client.embed(request);

            // Capture output using the visitor pattern for the union type
            response.visit(new EmbedResponse.Visitor<Void>() {
                @Override
                public Void visitEmbeddingsFloats(EmbedFloatsResponse floatsResponse) {
                    List<List<Double>> embeddings = floatsResponse.getEmbeddings();
                    if (embeddings != null && !embeddings.isEmpty()) {
                        span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) embeddings.size());
                        span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embeddings.get(0).size());
                    }
                    // Token usage from float response meta
                    floatsResponse.getMeta().ifPresent(meta ->
                        meta.getBilledUnits().ifPresent(units ->
                            units.getInputTokens().ifPresent(inputTokens ->
                                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
                                    inputTokens.longValue()))));
                    return null;
                }

                @Override
                public Void visitEmbeddingsByType(EmbedByTypeResponse byTypeResponse) {
                    // Handle by-type response
                    return null;
                }

                @Override
                public Void _visitUnknown(Object unknown) {
                    return null;
                }
            });

            tracer.setRawOutput(span, response);
            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Cohere embed failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Reranks documents with tracing.
     *
     * @param request the rerank request
     * @return the rerank response
     */
    public RerankResponse rerank(RerankRequest request) {
        Span span = tracer.startSpan("Cohere Rerank", FISpanKind.RERANKER);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "cohere");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "cohere");

            // Set model
            request.getModel().ifPresent(model ->
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model));

            // Capture query
            tracer.setInputValue(span, request.getQuery());
            span.setAttribute("cohere.rerank.query", request.getQuery());

            // Capture documents count
            if (request.getDocuments() != null) {
                span.setAttribute("cohere.rerank.documents_count", (long) request.getDocuments().size());
            }

            // Capture top_n
            request.getTopN().ifPresent(topN ->
                span.setAttribute("cohere.rerank.top_n", topN.longValue()));

            tracer.setRawInput(span, request);

            // Execute request
            RerankResponse response = client.rerank(request);

            // Capture output
            if (response.getResults() != null) {
                span.setAttribute("cohere.rerank.results_count", (long) response.getResults().size());

                // Capture top result
                if (!response.getResults().isEmpty()) {
                    RerankResponseResultsItem topResult = response.getResults().get(0);
                    span.setAttribute("cohere.rerank.top_score", (double) topResult.getRelevanceScore());
                    span.setAttribute("cohere.rerank.top_index", (long) topResult.getIndex());
                }
            }

            // Token usage
            response.getMeta().ifPresent(meta ->
                meta.getBilledUnits().ifPresent(units ->
                    units.getSearchUnits().ifPresent(searchUnits ->
                        span.setAttribute("cohere.rerank.search_units", searchUnits.longValue()))));

            tracer.setRawOutput(span, response);
            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Cohere rerank failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying Cohere client.
     *
     * @return the wrapped Cohere client
     */
    public Cohere unwrap() {
        return client;
    }

    private String extractMessageRole(Message message) {
        if (message.isUser()) return "user";
        if (message.isChatbot()) return "chatbot";
        if (message.isSystem()) return "system";
        if (message.isTool()) return "tool";
        return "unknown";
    }

    private String extractMessageContent(Message message) {
        return message.visit(new Message.Visitor<String>() {
            @Override
            public String visitUser(ChatMessage userMessage) {
                return userMessage.getMessage();
            }

            @Override
            public String visitChatbot(ChatMessage chatbotMessage) {
                return chatbotMessage.getMessage();
            }

            @Override
            public String visitSystem(ChatMessage systemMessage) {
                return systemMessage.getMessage();
            }

            @Override
            public String visitTool(ChatToolMessage toolMessage) {
                return toolMessage.getToolResults()
                    .map(results -> tracer.toJson(results))
                    .orElse("");
            }

            @Override
            public String _visitUnknown(Object unknown) {
                return unknown != null ? unknown.toString() : "";
            }
        });
    }
}
