package ai.traceai.bedrock;

import ai.traceai.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

/**
 * Instrumentation wrapper for AWS Bedrock Runtime client.
 * Wraps the Bedrock client to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * BedrockRuntimeClient bedrock = BedrockRuntimeClient.create();
 * TracedBedrockRuntimeClient traced = new TracedBedrockRuntimeClient(bedrock);
 *
 * InvokeModelResponse response = traced.invokeModel(InvokeModelRequest.builder()
 *     .modelId("anthropic.claude-v2")
 *     .body(SdkBytes.fromUtf8String("{\"prompt\": \"Hello!\", \"max_tokens_to_sample\": 200}"))
 *     .build());
 * </pre>
 */
public class TracedBedrockRuntimeClient {

    private final BedrockRuntimeClient client;
    private final FITracer tracer;
    private final Gson gson;

    /**
     * Creates a new traced Bedrock client with the given client and tracer.
     *
     * @param client the Bedrock client to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedBedrockRuntimeClient(BedrockRuntimeClient client, FITracer tracer) {
        this.client = client;
        this.tracer = tracer;
        this.gson = new Gson();
    }

    /**
     * Creates a new traced Bedrock client using the global TraceAI tracer.
     *
     * @param client the Bedrock client to wrap
     */
    public TracedBedrockRuntimeClient(BedrockRuntimeClient client) {
        this(client, TraceAI.getTracer());
    }

    /**
     * Invokes a model with tracing.
     *
     * @param request the invoke model request
     * @return the invoke model response
     */
    public InvokeModelResponse invokeModel(InvokeModelRequest request) {
        String modelId = request.modelId();
        String provider = extractProvider(modelId);

        Span span = tracer.startSpan("Bedrock Invoke Model", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "bedrock");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelId);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelId);

            // Parse and capture input
            String inputJson = request.body().asUtf8String();
            tracer.setRawInput(span, inputJson);

            // Extract input based on provider format
            captureInput(span, inputJson, provider);

            // Execute request
            InvokeModelResponse response = client.invokeModel(request);

            // Parse and capture output
            String outputJson = response.body().asUtf8String();
            tracer.setRawOutput(span, outputJson);

            // Extract output based on provider format
            captureOutput(span, outputJson, provider);

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
     * Invokes a model with conversation API with tracing.
     *
     * @param request the converse request
     * @return the converse response
     */
    public ConverseResponse converse(ConverseRequest request) {
        String modelId = request.modelId();
        String provider = extractProvider(modelId);

        Span span = tracer.startSpan("Bedrock Converse", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "bedrock");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, provider);
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelId);

            // Capture input messages
            List<Map<String, String>> inputMsgs = new ArrayList<>();

            // System prompt goes first
            if (request.system() != null && !request.system().isEmpty()) {
                StringBuilder systemPrompt = new StringBuilder();
                for (SystemContentBlock block : request.system()) {
                    if (block.text() != null) {
                        systemPrompt.append(block.text());
                    }
                }
                inputMsgs.add(FITracer.message("system", systemPrompt.toString()));
            }

            if (request.messages() != null) {
                for (int i = 0; i < request.messages().size(); i++) {
                    Message msg = request.messages().get(i);
                    String role = msg.roleAsString();
                    String content = extractMessageContent(msg);
                    inputMsgs.add(FITracer.message(role, content));
                }
            }

            tracer.setInputMessages(span, inputMsgs);

            // Capture inference config
            if (request.inferenceConfig() != null) {
                InferenceConfiguration config = request.inferenceConfig();
                if (config.maxTokens() != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_MAX_TOKENS, config.maxTokens().longValue());
                }
                if (config.temperature() != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TEMPERATURE, config.temperature());
                }
                if (config.topP() != null) {
                    span.setAttribute(SemanticConventions.LLM_REQUEST_TOP_P, config.topP());
                }
            }

            tracer.setRawInput(span, request);

            // Execute request
            ConverseResponse response = client.converse(request);

            // Capture output
            if (response.output() != null && response.output().message() != null) {
                Message outputMsg = response.output().message();
                String role = outputMsg.roleAsString();
                String content = extractMessageContent(outputMsg);
                tracer.setOutputValue(span, content);
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message(role, content)));
            }

            // Capture stop reason
            if (response.stopReason() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, response.stopReasonAsString());
            }

            // Capture usage
            if (response.usage() != null) {
                TokenUsage usage = response.usage();
                tracer.setTokenCounts(
                    span,
                    usage.inputTokens(),
                    usage.outputTokens(),
                    usage.totalTokens()
                );
            }

            tracer.setRawOutput(span, response);

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
     * Gets the underlying Bedrock client.
     *
     * @return the wrapped BedrockRuntimeClient
     */
    public BedrockRuntimeClient unwrap() {
        return client;
    }

    private String extractProvider(String modelId) {
        if (modelId == null) return "unknown";

        if (modelId.startsWith("anthropic.")) return "anthropic";
        if (modelId.startsWith("amazon.")) return "amazon";
        if (modelId.startsWith("ai21.")) return "ai21";
        if (modelId.startsWith("cohere.")) return "cohere";
        if (modelId.startsWith("meta.")) return "meta";
        if (modelId.startsWith("mistral.")) return "mistral";
        if (modelId.startsWith("stability.")) return "stability";

        return "unknown";
    }

    private void captureInput(Span span, String inputJson, String provider) {
        try {
            JsonObject json = JsonParser.parseString(inputJson).getAsJsonObject();

            switch (provider) {
                case "anthropic":
                    if (json.has("prompt")) {
                        tracer.setInputValue(span, json.get("prompt").getAsString());
                    }
                    if (json.has("messages")) {
                        // Claude Messages API format
                        var messages = json.getAsJsonArray("messages");
                        List<Map<String, String>> inputMsgs = new ArrayList<>();
                        for (int i = 0; i < messages.size(); i++) {
                            var msg = messages.get(i).getAsJsonObject();
                            String role = msg.has("role") ? msg.get("role").getAsString() : "user";
                            String content = msg.has("content") ? msg.get("content").getAsString() : "";
                            inputMsgs.add(FITracer.message(role, content));
                        }
                        tracer.setInputMessages(span, inputMsgs);
                    }
                    break;

                case "amazon":
                    if (json.has("inputText")) {
                        tracer.setInputValue(span, json.get("inputText").getAsString());
                    }
                    break;

                case "meta":
                    if (json.has("prompt")) {
                        tracer.setInputValue(span, json.get("prompt").getAsString());
                    }
                    break;

                default:
                    // Generic capture
                    if (json.has("prompt")) {
                        tracer.setInputValue(span, json.get("prompt").getAsString());
                    } else if (json.has("input")) {
                        tracer.setInputValue(span, json.get("input").getAsString());
                    }
            }
        } catch (Exception e) {
            // Ignore parsing errors
        }
    }

    private void captureOutput(Span span, String outputJson, String provider) {
        try {
            JsonObject json = JsonParser.parseString(outputJson).getAsJsonObject();

            switch (provider) {
                case "anthropic":
                    if (json.has("completion")) {
                        tracer.setOutputValue(span, json.get("completion").getAsString());
                    }
                    if (json.has("content")) {
                        // Claude Messages API format
                        var content = json.getAsJsonArray("content");
                        if (content.size() > 0) {
                            var block = content.get(0).getAsJsonObject();
                            if (block.has("text")) {
                                tracer.setOutputValue(span, block.get("text").getAsString());
                            }
                        }
                    }
                    if (json.has("stop_reason")) {
                        span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                            json.get("stop_reason").getAsString());
                    }
                    break;

                case "amazon":
                    if (json.has("results")) {
                        var results = json.getAsJsonArray("results");
                        if (results.size() > 0) {
                            var result = results.get(0).getAsJsonObject();
                            if (result.has("outputText")) {
                                tracer.setOutputValue(span, result.get("outputText").getAsString());
                            }
                        }
                    }
                    break;

                case "meta":
                    if (json.has("generation")) {
                        tracer.setOutputValue(span, json.get("generation").getAsString());
                    }
                    break;

                default:
                    // Generic capture
                    if (json.has("completion")) {
                        tracer.setOutputValue(span, json.get("completion").getAsString());
                    } else if (json.has("output")) {
                        tracer.setOutputValue(span, json.get("output").getAsString());
                    } else if (json.has("generation")) {
                        tracer.setOutputValue(span, json.get("generation").getAsString());
                    }
            }

            // Capture token usage if available
            if (json.has("usage")) {
                var usage = json.getAsJsonObject("usage");
                int inputTokens = usage.has("input_tokens") ? usage.get("input_tokens").getAsInt() : 0;
                int outputTokens = usage.has("output_tokens") ? usage.get("output_tokens").getAsInt() : 0;
                tracer.setTokenCounts(span, inputTokens, outputTokens, inputTokens + outputTokens);
            }

        } catch (Exception e) {
            // Ignore parsing errors
        }
    }

    private String extractMessageContent(Message message) {
        if (message.content() == null || message.content().isEmpty()) {
            return "";
        }

        StringBuilder sb = new StringBuilder();
        for (ContentBlock block : message.content()) {
            if (block.text() != null) {
                sb.append(block.text());
            }
        }
        return sb.toString();
    }
}
