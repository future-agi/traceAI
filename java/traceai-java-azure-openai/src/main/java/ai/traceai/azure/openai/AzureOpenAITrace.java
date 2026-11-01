package ai.traceai.azure.openai;

import ai.traceai.*;
import com.azure.ai.openai.models.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Per-request semantic state shared by blocking, streaming and reactive clients. */
final class AzureOpenAITrace {
    private final FITracer tracer;
    final Span span;
    private final AtomicBoolean ended = new AtomicBoolean();
    private final Map<Integer, StringBuilder> text = new TreeMap<>();
    private int remaining = 32000;

    AzureOpenAITrace(FITracer tracer, String operation, FISpanKind kind, Context parent) {
        this.tracer = tracer;
        this.span = tracer.startSpan("Azure OpenAI " + operation, kind, parent);
    }

    // Telemetry must not replace a successful response or an SDK exception.
    void capture(Runnable action) {
        try { action.run(); } catch (RuntimeException ignored) { /* best-effort telemetry */ }
    }

    synchronized void end(Throwable error, boolean cancelled) {
        if (!ended.compareAndSet(false, true)) return;
        try {
            capture(() -> {
                if (!text.isEmpty()) {
                    List<Map<String, String>> messages = new ArrayList<>();
                    text.values().forEach(value -> messages.add(FITracer.message("assistant", value.toString())));
                    tracer.setOutputMessages(span, messages);
                    StringBuilder primary = text.get(0);
                    if (primary != null) tracer.setOutputValue(span, primary.toString());
                }
            });
            capture(() -> {
                if (error != null) tracer.setError(span, error);
                else if (cancelled) span.setAttribute("azure.openai.cancelled", true);
                else span.setStatus(StatusCode.OK);
            });
        } finally {
            text.clear();
            span.end();
        }
    }

    synchronized void chunk(ChatCompletions chunk) {
        if (ended.get()) return;
        if (chunk.getId() != null) span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, chunk.getId());
        if (chunk.getModel() != null) span.setAttribute(SemanticConventions.LLM_RESPONSE_MODEL, chunk.getModel());
        usage(chunk.getUsage());
        if (chunk.getChoices() != null) for (ChatChoice choice : chunk.getChoices()) {
            if (choice.getFinishReason() != null)
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, choice.getFinishReason().toString());
            if (choice.getDelta() != null) append(choice.getIndex(), choice.getDelta().getContent());
        }
    }

    synchronized void chunk(Completions chunk) {
        if (ended.get()) return;
        if (chunk.getId() != null) span.setAttribute(SemanticConventions.LLM_RESPONSE_ID, chunk.getId());
        usage(chunk.getUsage());
        if (chunk.getChoices() != null) for (Choice choice : chunk.getChoices()) {
            if (choice.getFinishReason() != null)
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, choice.getFinishReason().toString());
            append(choice.getIndex(), choice.getText());
        }
    }

    private void usage(CompletionsUsage usage) {
        if (usage != null) tracer.setTokenCounts(span, usage.getPromptTokens(), usage.getCompletionTokens(), usage.getTotalTokens());
    }

    private void append(int index, String value) {
        // Bound retained content across all choices; never retain hidden output.
        if (tracer.getConfig().isHideOutputs() || value == null || value.isEmpty()) return;
        if (value.length() > remaining) span.setAttribute("azure.openai.output.truncated", true);
        if (remaining == 0) return;
        int length = Math.min(remaining, value.length());
        text.computeIfAbsent(index, ignored -> new StringBuilder()).append(value, 0, length);
        remaining -= length;
    }

    void request(String deploymentOrModelName, ChatCompletionsOptions options) {
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

    }

    void response(ChatCompletions result) {
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

    }

    void request(String deploymentOrModelName, EmbeddingsOptions options) {
        // Set system attributes
        span.setAttribute(SemanticConventions.LLM_SYSTEM, "azure-openai");
        span.setAttribute(SemanticConventions.LLM_PROVIDER, "azure");
        span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, deploymentOrModelName);

        // Capture raw input
        tracer.setRawInput(span, options);

    }

    void response(Embeddings result) {
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

    }

    void request(String deploymentOrModelName, CompletionsOptions options) {
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

    }

    void response(Completions result) {
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
            if (!tracer.getConfig().isHideOutputs() && !tracer.getConfig().isHideOutputMessages()
                && message.getToolCalls() != null && !message.getToolCalls().isEmpty()) {
                for (int i = 0; i < message.getToolCalls().size(); i++) {
                    ChatCompletionsToolCall toolCall = message.getToolCalls().get(i);
                    if (toolCall instanceof ChatCompletionsFunctionToolCall) {
                        ChatCompletionsFunctionToolCall functionToolCall = (ChatCompletionsFunctionToolCall) toolCall;
                        span.setAttribute(SemanticConventions.LLM_OUTPUT_MESSAGES + "." + index
                            + "." + SemanticConventions.MESSAGE_TOOL_CALLS + "." + i + "."
                            + SemanticConventions.TOOL_CALL_ID, functionToolCall.getId());
                        if (functionToolCall.getFunction() != null) {
                            span.setAttribute(SemanticConventions.LLM_OUTPUT_MESSAGES + "." + index
                                + "." + SemanticConventions.MESSAGE_TOOL_CALLS + "." + i + "."
                                + SemanticConventions.TOOL_CALL_FUNCTION_NAME,
                                functionToolCall.getFunction().getName());
                            span.setAttribute(SemanticConventions.LLM_OUTPUT_MESSAGES + "." + index
                                + "." + SemanticConventions.MESSAGE_TOOL_CALLS + "." + i + "."
                                + SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS,
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
