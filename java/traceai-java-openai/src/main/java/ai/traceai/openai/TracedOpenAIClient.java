package ai.traceai.openai;

import ai.traceai.*;
import com.openai.client.OpenAIClient;
import com.openai.models.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.List;
import java.util.stream.Stream;

/**
 * Instrumentation wrapper for OpenAI Java client.
 * Wraps the OpenAI client to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * OpenAIClient openai = OpenAI.client();
 * TracedOpenAIClient traced = new TracedOpenAIClient(openai);
 *
 * ChatCompletion response = traced.createChatCompletion(
 *     ChatCompletionCreateParams.builder()
 *         .model("gpt-4")
 *         .addMessage(ChatCompletionMessageParam.user("Hello!"))
 *         .build()
 * );
 * </pre>
 */
public class TracedOpenAIClient {

    private final OpenAIClient client;
    private final FITracer tracer;

    /**
     * Creates a new traced OpenAI client with the given client and tracer.
     *
     * @param client the OpenAI client to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedOpenAIClient(OpenAIClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced OpenAI client using the global TraceAI tracer.
     *
     * @param client the OpenAI client to wrap
     */
    public TracedOpenAIClient(OpenAIClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Creates a chat completion with tracing.
     *
     * @param params the chat completion parameters
     * @return the chat completion response
     */
    public ChatCompletion createChatCompletion(ChatCompletionCreateParams params) {
        String model = params.model().toString();

        Span span = tracer.startSpan("OpenAI Chat Completion", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "openai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "openai");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, model);

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

            // Capture input messages
            List<ChatCompletionMessageParam> messages = params.messages();
            for (int i = 0; i < messages.size(); i++) {
                ChatCompletionMessageParam msg = messages.get(i);
                captureInputMessage(span, i, msg);
            }

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

            // Capture output messages
            if (result.choices() != null && !result.choices().isEmpty()) {
                for (int i = 0; i < result.choices().size(); i++) {
                    ChatCompletion.Choice choice = result.choices().get(i);
                    captureOutputChoice(span, i, choice);
                }

                // Set primary output value
                ChatCompletion.Choice firstChoice = result.choices().get(0);
                if (firstChoice.message() != null && firstChoice.message().content().isPresent()) {
                    tracer.setOutputValue(span, firstChoice.message().content().get());
                }
            }

            // Token usage
            if (result.usage() != null) {
                tracer.setTokenCounts(
                    span,
                    result.usage().promptTokens().intValue(),
                    result.usage().completionTokens().intValue(),
                    result.usage().totalTokens().intValue()
                );
            }

            // Capture raw output
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
     *
     * @param params the embedding parameters
     * @return the embedding response
     */
    public CreateEmbeddingResponse createEmbedding(EmbeddingCreateParams params) {
        Span span = tracer.startSpan("OpenAI Embedding", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "openai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "openai");
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, params.model().toString());

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
                span.setAttribute(
                    SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
                    result.usage().promptTokens().longValue()
                );
                span.setAttribute(
                    SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
                    result.usage().totalTokens().longValue()
                );
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
     * Creates a streaming chat completion with tracing.
     * The span is completed when the stream is fully consumed.
     *
     * @param params the chat completion parameters
     * @return a stream of chat completion chunks
     */
    public Stream<ChatCompletionChunk> streamChatCompletion(ChatCompletionCreateParams params) {
        String model = params.model().toString();

        Span span = tracer.startSpan("OpenAI Chat Completion (Stream)", FISpanKind.LLM);

        try {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "openai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "openai");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);

            // Capture input messages
            List<ChatCompletionMessageParam> messages = params.messages();
            for (int i = 0; i < messages.size(); i++) {
                ChatCompletionMessageParam msg = messages.get(i);
                captureInputMessage(span, i, msg);
            }

            tracer.setRawInput(span, params);

            // Get the stream
            Stream<ChatCompletionChunk> stream = client.chat().completions().createStreaming(params);

            // Wrap the stream to capture output and close span
            StringBuilder content = new StringBuilder();

            return stream.peek(chunk -> {
                if (chunk.choices() != null && !chunk.choices().isEmpty()) {
                    ChatCompletionChunk.Choice choice = chunk.choices().get(0);
                    if (choice.delta() != null && choice.delta().content().isPresent()) {
                        content.append(choice.delta().content().get());
                    }

                    // Check if this is the final chunk
                    if (choice.finishReason() != null) {
                        tracer.setOutputValue(span, content.toString());
                        tracer.setOutputMessage(span, 0, "assistant", content.toString());
                        span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                            choice.finishReason().toString());
                    }
                }

                // Capture usage if present
                if (chunk.usage() != null) {
                    tracer.setTokenCounts(
                        span,
                        chunk.usage().promptTokens().intValue(),
                        chunk.usage().completionTokens().intValue(),
                        chunk.usage().totalTokens().intValue()
                    );
                }
            }).onClose(() -> {
                span.setStatus(StatusCode.OK);
                span.end();
            });

        } catch (Exception e) {
            tracer.setError(span, e);
            span.end();
            throw e;
        }
    }

    /**
     * Gets the underlying OpenAI client.
     *
     * @return the wrapped OpenAI client
     */
    public OpenAIClient unwrap() {
        return client;
    }

    private void captureInputMessage(Span span, int index, ChatCompletionMessageParam msg) {
        String role = msg.role().toString();
        tracer.setInputMessage(span, index, role, extractMessageContent(msg));
    }

    private void captureOutputChoice(Span span, int index, ChatCompletion.Choice choice) {
        if (choice.message() != null) {
            String role = choice.message().role().toString();
            String content = choice.message().content().orElse(null);
            tracer.setOutputMessage(span, index, role, content);

            // Capture finish reason
            if (choice.finishReason() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                    choice.finishReason().toString());
            }

            // Capture tool calls if present
            if (choice.message().toolCalls() != null && !choice.message().toolCalls().isEmpty()) {
                for (int i = 0; i < choice.message().toolCalls().size(); i++) {
                    ChatCompletionMessageToolCall toolCall = choice.message().toolCalls().get(i);
                    span.setAttribute("llm.output_messages." + index + ".tool_calls." + i + ".id",
                        toolCall.id());
                    span.setAttribute("llm.output_messages." + index + ".tool_calls." + i + ".function.name",
                        toolCall.function().name());
                    span.setAttribute("llm.output_messages." + index + ".tool_calls." + i + ".function.arguments",
                        toolCall.function().arguments());
                }
            }
        }
    }

    private String extractMessageContent(ChatCompletionMessageParam msg) {
        // The OpenAI Java SDK uses a union type for message content
        // We need to handle different message types appropriately
        try {
            // Attempt to get content - implementation depends on SDK version
            return msg.toString();
        } catch (Exception e) {
            return "[content extraction failed]";
        }
    }
}
