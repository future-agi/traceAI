package ai.traceai.openai;

import ai.traceai.*;
import com.openai.client.OpenAIClient;
import com.openai.models.*;
import com.openai.core.http.StreamResponse;
import io.opentelemetry.context.Context;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Legacy facade for OpenAI Java 0.8 chat completions and embeddings.
 * New integrations should use {@link TracingHttpClient} with the SDK's native services; this
 * facade retains its original model types and method descriptors for existing customers.
 *
 * <p>Usage:</p>
 * <pre>
 * // existingClient and params use the OpenAI Java 0.8 API.
 * TracedOpenAIClient traced = new TracedOpenAIClient(existingClient);
 * ChatCompletion response = traced.createChatCompletion(params);
 * </pre>
 */
public class TracedOpenAIClient {

    private final OpenAIClient client;
    private final FITracer tracer;

    public TracedOpenAIClient(OpenAIClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    public TracedOpenAIClient(OpenAIClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Creates a chat completion with tracing.
     */
    public ChatCompletion createChatCompletion(ChatCompletionCreateParams params) {
        String model = params.model().toString();

        Span span = tracer.startSpan("OpenAI Chat Completion", FISpanKind.LLM);

        try (Scope scope = Context.current().with(span).with(OpenAITrace.LEGACY, true).makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "openai");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, model);
            span.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, "chat");

            // Set request parameters
            params.temperature().ifPresent(temp ->
                span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, temp)
            );
            params.topP().ifPresent(topP ->
                span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, topP)
            );
            params.maxTokens().ifPresent(maxTokens ->
                span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, maxTokens.longValue())
            );

            // Capture input messages as JSON blob
            List<ChatCompletionMessageParam> messages = params.messages();
            List<Map<String, String>> inputMessages = new ArrayList<>();
            for (ChatCompletionMessageParam msg : messages) {
                inputMessages.add(FITracer.message("user", extractMessageContent(msg)));
            }
            tracer.setInputMessages(span, inputMessages);

            // Capture raw input
            tracer.setRawInput(span, params);

            // Execute request
            ChatCompletion result = client.chat().completions().create(params);

            // Set response model
            if (result.model() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, result.model());
            }

            // Set response ID
            if (result.id() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, result.id());
            }

            // Capture output messages as JSON blob
            if (result.choices() != null && !result.choices().isEmpty()) {
                List<Map<String, String>> outputMessages = new ArrayList<>();
                for (ChatCompletion.Choice choice : result.choices()) {
                    if (choice.message() != null) {
                        String role = choice.message().role().toString();
                        String content = choice.message().content().orElse(null);
                        outputMessages.add(FITracer.message(role, content));

                        if (choice.finishReason() != null) {
                            span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                                choice.finishReason().toString());
                        }
                    }
                }
                tracer.setOutputMessages(span, outputMessages);

                // Set primary output value
                ChatCompletion.Choice firstChoice = result.choices().get(0);
                if (firstChoice.message() != null && firstChoice.message().content().isPresent()) {
                    tracer.setOutputValue(span, firstChoice.message().content().get());
                }
            }

            // Token usage — usage() returns Optional<CompletionUsage>
            result.usage().ifPresent(usage -> {
                tracer.setTokenCounts(span,
                    usage.promptTokens(),
                    usage.completionTokens(),
                    usage.totalTokens()
                );
            });

            tracer.setRawOutput(span, result);
            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Creates an embedding with tracing.
     */
    public CreateEmbeddingResponse createEmbedding(EmbeddingCreateParams params) {
        Span span = tracer.startSpan("OpenAI Embedding", FISpanKind.EMBEDDING);

        try (Scope scope = Context.current().with(span).with(OpenAITrace.LEGACY, true).makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "openai");
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, params.model().toString());
            span.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, "embeddings");

            tracer.setRawInput(span, params);

            CreateEmbeddingResponse result = client.embeddings().create(params);

            if (result.data() != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) result.data().size());
                if (!result.data().isEmpty()) {
                    span.setAttribute(
                        SemanticConventions.EMBEDDING_DIMENSIONS,
                        (long) result.data().get(0).embedding().size()
                    );
                }
            }

            if (result.usage() != null) {
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
                    result.usage().promptTokens());
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
                    result.usage().totalTokens());
            }

            tracer.setRawOutput(span, result);
            span.setStatus(StatusCode.OK);
            return result;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Returns a lazy, single-use iterable. Complete iteration closes the response.
     * @deprecated Use {@link #streamChatCompletionResponse} in try-with-resources so breaking out
     *     of iteration also closes the response. The return type is retained for binary compatibility.
     */
    @Deprecated
    public Iterable<ChatCompletionChunk> streamChatCompletion(ChatCompletionCreateParams params) throws Exception {
        return (LegacyChatStream) streamChatCompletionResponse(params);
    }

    /**
     * Creates a lazy, closeable streaming response for the legacy OpenAI Java 0.8 API.
     * Always close it, including when taking only the first chunk or cancelling generation.
     * For current SDK clients, install {@link TracingHttpClient} and use the SDK's own stream API.
     * @param params the chat completion request
     * @return the response, owned by the caller
     */
    public StreamResponse<ChatCompletionChunk> streamChatCompletionResponse(ChatCompletionCreateParams params) {
        Span span = tracer.startSpan("OpenAI Chat Completion (Stream)", FISpanKind.LLM);
        try (Scope scope = Context.current().with(span).with(OpenAITrace.LEGACY, true).makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "openai");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, params.model().toString());
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, params.model().toString());
            span.setAttribute(SemanticConventions.GEN_AI_OPERATION_NAME, "chat");
            List<Map<String, String>> inputMessages = new ArrayList<>();
            for (ChatCompletionMessageParam message : params.messages()) {
                inputMessages.add(FITracer.message("user", extractMessageContent(message)));
            }
            tracer.setInputMessages(span, inputMessages);
            if (!tracer.getConfig().isHideInputMessages()) tracer.setRawInput(span, params);
            return new LegacyChatStream(client.chat().completions().createStreaming(params), span, tracer);
        } catch (Throwable error) {
            tracer.setError(span, error);
            span.end();
            throw OpenAITrace.propagate(error);
        }
    }

    public OpenAIClient unwrap() {
        return client;
    }

    private String extractMessageContent(ChatCompletionMessageParam msg) {
        try {
            return msg.toString();
        } catch (Exception e) {
            return "[content extraction failed]";
        }
    }
}
