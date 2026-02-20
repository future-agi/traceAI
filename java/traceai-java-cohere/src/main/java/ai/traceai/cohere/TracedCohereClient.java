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
            if (request.getModel().isPresent()) {
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, request.getModel().get());
                span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, request.getModel().get());
            }

            // Set request parameters
            if (request.getTemperature().isPresent()) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, request.getTemperature().get());
            }
            if (request.getMaxTokens().isPresent()) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, request.getMaxTokens().get().longValue());
            }

            // Capture input message
            tracer.setInputValue(span, request.getMessage());
            List<Map<String, String>> inputMessages = new ArrayList<>();
            inputMessages.add(FITracer.message("user", request.getMessage()));

            // Capture chat history if present
            if (request.getChatHistory().isPresent()) {
                List<Message> history = request.getChatHistory().get();
                for (Message msg : history) {
                    String role = msg.getRole().toString().toLowerCase();
                    String content = extractMessageContent(msg);
                    inputMessages.add(FITracer.message(role, content));
                }
            }
            tracer.setInputMessages(span, inputMessages);

            // Capture preamble/system prompt if present
            if (request.getPreamble().isPresent()) {
                span.setAttribute("cohere.preamble", request.getPreamble().get());
            }

            tracer.setRawInput(span, request);

            // Execute request
            NonStreamedChatResponse response = client.chat(request);

            // Capture output
            if (response.getText() != null) {
                tracer.setOutputValue(span, response.getText());
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", response.getText())));
            }

            // Capture finish reason
            if (response.getFinishReason() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                    response.getFinishReason().toString());
            }

            // Capture response ID
            if (response.getGenerationId() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, response.getGenerationId());
            }

            // Token usage
            if (response.getMeta().isPresent()) {
                ApiMeta meta = response.getMeta().get();
                if (meta.getTokens().isPresent()) {
                    ApiMetaTokens tokens = meta.getTokens().get();
                    int inputTokens = tokens.getInputTokens().orElse(0);
                    int outputTokens = tokens.getOutputTokens().orElse(0);
                    tracer.setTokenCounts(span, inputTokens, outputTokens, inputTokens + outputTokens);
                }
            }

            // Capture tool calls if present
            if (response.getToolCalls() != null && !response.getToolCalls().isEmpty()) {
                for (int i = 0; i < response.getToolCalls().size(); i++) {
                    ToolCall toolCall = response.getToolCalls().get(i);
                    span.setAttribute("llm.tool_calls." + i + ".name", toolCall.getName());
                    span.setAttribute("llm.tool_calls." + i + ".parameters",
                        tracer.toJson(toolCall.getParameters()));
                }
            }

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
            if (request.getModel().isPresent()) {
                span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, request.getModel().get());
            }

            // Capture input texts
            List<String> texts = request.getTexts();
            if (texts != null) {
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
            }

            // Capture embedding type
            if (request.getInputType().isPresent()) {
                span.setAttribute("cohere.input_type", request.getInputType().get().toString());
            }

            tracer.setRawInput(span, request);

            // Execute request
            EmbedResponse response = client.embed(request);

            // Capture output
            if (response.getEmbeddings() != null) {
                // Handle different embedding response types
                response.getEmbeddings().visit(new EmbeddingsResponse.Visitor<Void>() {
                    @Override
                    public Void visitEmbeddingsFloatsResponse(EmbeddingsFloatsResponse floatsResponse) {
                        List<List<Double>> embeddings = floatsResponse.getEmbeddings();
                        if (embeddings != null && !embeddings.isEmpty()) {
                            span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) embeddings.size());
                            span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS, (long) embeddings.get(0).size());
                        }
                        return null;
                    }

                    @Override
                    public Void visitEmbeddingsByTypeResponse(EmbeddingsByTypeResponse byTypeResponse) {
                        // Handle by-type response
                        return null;
                    }

                    @Override
                    public Void _visitUnknown(Object unknown) {
                        return null;
                    }
                });
            }

            // Token usage
            if (response.getMeta().isPresent()) {
                ApiMeta meta = response.getMeta().get();
                if (meta.getBilledUnits().isPresent()) {
                    ApiMetaBilledUnits units = meta.getBilledUnits().get();
                    if (units.getInputTokens().isPresent()) {
                        span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
                            units.getInputTokens().get().longValue());
                    }
                }
            }

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
            if (request.getModel().isPresent()) {
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, request.getModel().get());
            }

            // Capture query
            tracer.setInputValue(span, request.getQuery());
            span.setAttribute("cohere.rerank.query", request.getQuery());

            // Capture documents count
            if (request.getDocuments() != null) {
                span.setAttribute("cohere.rerank.documents_count", (long) request.getDocuments().size());
            }

            // Capture top_n
            if (request.getTopN().isPresent()) {
                span.setAttribute("cohere.rerank.top_n", request.getTopN().get().longValue());
            }

            tracer.setRawInput(span, request);

            // Execute request
            RerankResponse response = client.rerank(request);

            // Capture output
            if (response.getResults() != null) {
                span.setAttribute("cohere.rerank.results_count", (long) response.getResults().size());

                // Capture top result
                if (!response.getResults().isEmpty()) {
                    RerankResponseResultsItem topResult = response.getResults().get(0);
                    span.setAttribute("cohere.rerank.top_score", topResult.getRelevanceScore());
                    span.setAttribute("cohere.rerank.top_index", (long) topResult.getIndex());
                }
            }

            // Token usage
            if (response.getMeta().isPresent()) {
                ApiMeta meta = response.getMeta().get();
                if (meta.getBilledUnits().isPresent()) {
                    ApiMetaBilledUnits units = meta.getBilledUnits().get();
                    if (units.getSearchUnits().isPresent()) {
                        span.setAttribute("cohere.rerank.search_units", units.getSearchUnits().get().longValue());
                    }
                }
            }

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

    private String extractMessageContent(Message message) {
        // Extract content based on message role
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
            public String visitTool(ToolMessage toolMessage) {
                return tracer.toJson(toolMessage.getToolResults());
            }

            @Override
            public String _visitUnknown(Object unknown) {
                return unknown != null ? unknown.toString() : "";
            }
        });
    }
}
