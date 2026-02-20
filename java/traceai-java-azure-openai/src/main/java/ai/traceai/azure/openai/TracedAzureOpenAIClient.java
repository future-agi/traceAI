package ai.traceai.azure.openai;

import ai.traceai.*;
import com.azure.ai.openai.OpenAIClient;
import com.azure.ai.openai.models.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Azure OpenAI Java client.
 * Wraps the Azure OpenAI client to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * OpenAIClient azureClient = new OpenAIClientBuilder()
 *     .endpoint("https://your-resource.openai.azure.com/")
 *     .credential(new AzureKeyCredential("your-api-key"))
 *     .buildClient();
 *
 * TracedAzureOpenAIClient traced = new TracedAzureOpenAIClient(azureClient);
 *
 * ChatCompletions response = traced.getChatCompletions(
 *     "gpt-4",
 *     new ChatCompletionsOptions(List.of(
 *         new ChatRequestUserMessage("Hello!")
 *     ))
 * );
 * </pre>
 */
public class TracedAzureOpenAIClient {

    private final OpenAIClient client;
    private final FITracer tracer;

    /**
     * Creates a new traced Azure OpenAI client with the given client and tracer.
     *
     * @param client the Azure OpenAI client to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedAzureOpenAIClient(OpenAIClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Azure OpenAI client using the global TraceAI tracer.
     *
     * @param client the Azure OpenAI client to wrap
     */
    public TracedAzureOpenAIClient(OpenAIClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Gets chat completions with tracing.
     *
     * @param deploymentOrModelName the deployment name or model name
     * @param options the chat completions options
     * @return the chat completions response
     */
    public ChatCompletions getChatCompletions(String deploymentOrModelName, ChatCompletionsOptions options) {
        Span span = tracer.startSpan("Azure OpenAI Chat Completion", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "azure-openai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "azure");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, deploymentOrModelName);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, deploymentOrModelName);

            // Set request parameters
            if (options.getTemperature() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, options.getTemperature());
            }
            if (options.getTopP() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, options.getTopP());
            }
            if (options.getMaxTokens() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, options.getMaxTokens().longValue());
            }

            // Capture input messages
            List<ChatRequestMessage> messages = options.getMessages();
            if (messages != null) {
                List<Map<String, String>> inputMessages = new ArrayList<>();
                for (ChatRequestMessage msg : messages) {
                    inputMessages.add(toMessageMap(msg));
                }
                tracer.setInputMessages(span, inputMessages);
            }

            // Capture raw input
            tracer.setRawInput(span, options);

            // Execute request
            ChatCompletions result = client.getChatCompletions(deploymentOrModelName, options);

            // Set response model
            if (result.getModel() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, result.getModel());
            }

            // Set response ID
            if (result.getId() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, result.getId());
            }

            // Capture output messages
            if (result.getChoices() != null && !result.getChoices().isEmpty()) {
                List<Map<String, String>> outputMessages = new ArrayList<>();
                for (int i = 0; i < result.getChoices().size(); i++) {
                    ChatChoice choice = result.getChoices().get(i);
                    captureOutputChoice(span, i, choice, outputMessages);
                }
                tracer.setOutputMessages(span, outputMessages);

                // Set primary output value
                ChatChoice firstChoice = result.getChoices().get(0);
                if (firstChoice.getMessage() != null && firstChoice.getMessage().getContent() != null) {
                    tracer.setOutputValue(span, firstChoice.getMessage().getContent());
                }
            }

            // Token usage
            if (result.getUsage() != null) {
                tracer.setTokenCounts(
                    span,
                    result.getUsage().getPromptTokens(),
                    result.getUsage().getCompletionTokens(),
                    result.getUsage().getTotalTokens()
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
     * Gets embeddings with tracing.
     *
     * @param deploymentOrModelName the deployment name or model name
     * @param options the embeddings options
     * @return the embeddings response
     */
    public Embeddings getEmbeddings(String deploymentOrModelName, EmbeddingsOptions options) {
        Span span = tracer.startSpan("Azure OpenAI Embedding", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "azure-openai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "azure");
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, deploymentOrModelName);

            // Capture raw input
            tracer.setRawInput(span, options);

            // Execute request
            Embeddings result = client.getEmbeddings(deploymentOrModelName, options);

            // Capture embedding metadata
            if (result.getData() != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) result.getData().size());
                if (!result.getData().isEmpty()) {
                    EmbeddingItem firstEmbedding = result.getData().get(0);
                    if (firstEmbedding.getEmbedding() != null) {
                        span.setAttribute(
                            SemanticConventions.EMBEDDING_DIMENSIONS,
                            (long) firstEmbedding.getEmbedding().size()
                        );
                    }
                }
            }

            // Token usage
            if (result.getUsage() != null) {
                span.setAttribute(
                    SemanticConventions.LLM_TOKEN_COUNT_PROMPT,
                    (long) result.getUsage().getPromptTokens()
                );
                span.setAttribute(
                    SemanticConventions.LLM_TOKEN_COUNT_TOTAL,
                    (long) result.getUsage().getTotalTokens()
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
     * Gets completions with tracing.
     *
     * @param deploymentOrModelName the deployment name or model name
     * @param options the completions options
     * @return the completions response
     */
    public Completions getCompletions(String deploymentOrModelName, CompletionsOptions options) {
        Span span = tracer.startSpan("Azure OpenAI Completion", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "azure-openai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "azure");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, deploymentOrModelName);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, deploymentOrModelName);

            // Set request parameters
            if (options.getTemperature() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, options.getTemperature());
            }
            if (options.getTopP() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, options.getTopP());
            }
            if (options.getMaxTokens() != null) {
                span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, options.getMaxTokens().longValue());
            }

            // Capture input prompts
            List<String> prompts = options.getPrompt();
            if (prompts != null && !prompts.isEmpty()) {
                List<Map<String, String>> inputMessages = new ArrayList<>();
                for (String prompt : prompts) {
                    inputMessages.add(FITracer.message("user", prompt));
                }
                tracer.setInputMessages(span, inputMessages);
            }

            // Capture raw input
            tracer.setRawInput(span, options);

            // Execute request
            Completions result = client.getCompletions(deploymentOrModelName, options);

            // Set response ID
            if (result.getId() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, result.getId());
            }

            // Capture output choices
            if (result.getChoices() != null && !result.getChoices().isEmpty()) {
                List<Map<String, String>> outputMessages = new ArrayList<>();
                for (Choice choice : result.getChoices()) {
                    if (choice.getText() != null) {
                        outputMessages.add(FITracer.message("assistant", choice.getText()));
                    }
                    if (choice.getFinishReason() != null) {
                        span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                            choice.getFinishReason().toString());
                    }
                }
                tracer.setOutputMessages(span, outputMessages);

                // Set primary output value
                Choice firstChoice = result.getChoices().get(0);
                if (firstChoice.getText() != null) {
                    tracer.setOutputValue(span, firstChoice.getText());
                }
            }

            // Token usage
            if (result.getUsage() != null) {
                tracer.setTokenCounts(
                    span,
                    result.getUsage().getPromptTokens(),
                    result.getUsage().getCompletionTokens(),
                    result.getUsage().getTotalTokens()
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
     * Gets the underlying Azure OpenAI client.
     *
     * @return the wrapped Azure OpenAI client
     */
    public OpenAIClient unwrap() {
        return client;
    }

    private Map<String, String> toMessageMap(ChatRequestMessage msg) {
        String role = getMessageRole(msg);
        String content = getMessageContent(msg);
        return FITracer.message(role, content);
    }

    private void captureOutputChoice(Span span, int index, ChatChoice choice,
                                     List<Map<String, String>> outputMessages) {
        if (choice.getMessage() != null) {
            ChatResponseMessage message = choice.getMessage();
            String role = message.getRole() != null ? message.getRole().toString() : "assistant";
            String content = message.getContent();
            outputMessages.add(FITracer.message(role, content));

            // Capture finish reason
            if (choice.getFinishReason() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                    choice.getFinishReason().toString());
            }

            // Capture tool calls if present
            if (message.getToolCalls() != null && !message.getToolCalls().isEmpty()) {
                for (int i = 0; i < message.getToolCalls().size(); i++) {
                    ChatCompletionsToolCall toolCall = message.getToolCalls().get(i);
                    if (toolCall instanceof ChatCompletionsFunctionToolCall) {
                        ChatCompletionsFunctionToolCall functionToolCall = (ChatCompletionsFunctionToolCall) toolCall;
                        span.setAttribute("llm.output_messages." + index + ".tool_calls." + i + ".id",
                            functionToolCall.getId());
                        if (functionToolCall.getFunction() != null) {
                            span.setAttribute("llm.output_messages." + index + ".tool_calls." + i + ".function.name",
                                functionToolCall.getFunction().getName());
                            span.setAttribute("llm.output_messages." + index + ".tool_calls." + i + ".function.arguments",
                                functionToolCall.getFunction().getArguments());
                        }
                    }
                }
            }
        }
    }

    private String getMessageRole(ChatRequestMessage msg) {
        if (msg instanceof ChatRequestSystemMessage) {
            return "system";
        } else if (msg instanceof ChatRequestUserMessage) {
            return "user";
        } else if (msg instanceof ChatRequestAssistantMessage) {
            return "assistant";
        } else if (msg instanceof ChatRequestToolMessage) {
            return "tool";
        } else if (msg instanceof ChatRequestFunctionMessage) {
            return "function";
        }
        return "unknown";
    }

    private String getMessageContent(ChatRequestMessage msg) {
        try {
            if (msg instanceof ChatRequestSystemMessage) {
                Object content = ((ChatRequestSystemMessage) msg).getContent();
                return content != null ? content.toString() : null;
            } else if (msg instanceof ChatRequestUserMessage) {
                ChatRequestUserMessage userMsg = (ChatRequestUserMessage) msg;
                Object content = userMsg.getContent();
                return content != null ? content.toString() : null;
            } else if (msg instanceof ChatRequestAssistantMessage) {
                Object content = ((ChatRequestAssistantMessage) msg).getContent();
                return content != null ? content.toString() : null;
            } else if (msg instanceof ChatRequestToolMessage) {
                Object content = ((ChatRequestToolMessage) msg).getContent();
                return content != null ? content.toString() : null;
            } else if (msg instanceof ChatRequestFunctionMessage) {
                Object content = ((ChatRequestFunctionMessage) msg).getContent();
                return content != null ? content.toString() : null;
            }
        } catch (Exception e) {
            // Fall through to default
        }
        return msg.toString();
    }
}
