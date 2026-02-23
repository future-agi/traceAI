package ai.traceai.googlegenai;

import ai.traceai.*;
import com.google.genai.Chat;
import com.google.genai.Client;
import com.google.genai.ResponseStream;
import com.google.genai.types.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Google Gen AI (Gemini) Java SDK.
 * Wraps a Google GenAI Client to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * Client client = Client.builder().apiKey("your-key").build();
 * TracedGenerativeModel traced = new TracedGenerativeModel(client, "gemini-1.5-pro");
 *
 * GenerateContentResponse response = traced.generateContent("Hello!");
 * </pre>
 */
public class TracedGenerativeModel {

    private final Client client;
    private final FITracer tracer;
    private final String modelName;

    /**
     * Creates a new traced generative model with the given client, tracer, and model name.
     *
     * @param client    the Google GenAI Client to wrap
     * @param tracer    the FITracer for instrumentation
     * @param modelName the model name for tracing and API calls
     */
    public TracedGenerativeModel(Client client, FITracer tracer, String modelName) {
        this.client = client;
        this.tracer = tracer;
        this.modelName = modelName;
    }

    /**
     * Creates a new traced generative model using the global TraceAI tracer.
     *
     * @param client    the Google GenAI Client to wrap
     * @param modelName the model name for tracing and API calls
     */
    public TracedGenerativeModel(Client client, String modelName) {
        this(client, TraceAI.getTracer(), modelName);
    }

    /**
     * Generates content with tracing.
     *
     * @param prompt the text prompt
     * @return the generate content response
     */
    public GenerateContentResponse generateContent(String prompt) {
        Span span = tracer.startSpan("Google GenAI Generate Content", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            setSystemAttributes(span);

            tracer.setInputValue(span, prompt);
            tracer.setInputMessages(span, Collections.singletonList(FITracer.message("user", prompt)));

            GenerateContentResponse response = client.models.generateContent(modelName, prompt, null);

            captureResponse(span, response);

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Google GenAI generate content failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Generates content with tracing using Content objects.
     *
     * @param contents the input contents
     * @return the generate content response
     */
    public GenerateContentResponse generateContent(List<Content> contents) {
        Span span = tracer.startSpan("Google GenAI Generate Content", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            setSystemAttributes(span);

            List<Map<String, String>> inputMessages = new ArrayList<>();
            for (Content content : contents) {
                String role = content.role().orElse("user");
                String text = extractContentText(content);
                inputMessages.add(FITracer.message(role, text));
            }
            tracer.setInputMessages(span, inputMessages);

            tracer.setRawInput(span, contents);

            GenerateContentResponse response = client.models.generateContent(modelName, contents, null);

            captureResponse(span, response);

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Google GenAI generate content failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Counts tokens with tracing.
     *
     * @param prompt the text to count tokens for
     * @return the count tokens response
     */
    public CountTokensResponse countTokens(String prompt) {
        Span span = tracer.startSpan("Google GenAI Count Tokens", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "google-genai");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);

            tracer.setInputValue(span, prompt);

            CountTokensResponse response = client.models.countTokens(modelName, prompt, null);

            response.totalTokens().ifPresent(total ->
                span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, total.longValue())
            );

            span.setStatus(StatusCode.OK);
            return response;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw new RuntimeException("Google GenAI count tokens failed", e);
        } finally {
            span.end();
        }
    }

    /**
     * Starts a chat session with tracing support.
     *
     * @return a traced chat session
     */
    public TracedChat startChat() {
        Chat chat = client.chats.create(modelName);
        return new TracedChat(chat, tracer, modelName);
    }

    /**
     * Gets the underlying Client.
     *
     * @return the wrapped Client
     */
    public Client unwrap() {
        return client;
    }

    /**
     * Gets the model name used by this traced model.
     *
     * @return the model name
     */
    public String getModelName() {
        return modelName;
    }

    private void setSystemAttributes(Span span) {
        span.setAttribute(SemanticConventions.LLM_SYSTEM, "google-genai");
        span.setAttribute(SemanticConventions.LLM_PROVIDER, "google");
        span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);
        span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelName);
    }

    private void captureResponse(Span span, GenerateContentResponse response) {
        if (response == null) return;

        // Capture text output using the convenience method
        try {
            String text = response.text();
            if (text != null) {
                tracer.setOutputValue(span, text);
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("model", text)));
            }
        } catch (Exception e) {
            // text() may throw if no text candidates exist
        }

        // Capture finish reason from first candidate
        response.candidates().ifPresent(candidates -> {
            if (!candidates.isEmpty()) {
                Candidate firstCandidate = candidates.get(0);
                firstCandidate.finishReason().ifPresent(reason ->
                    span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON, reason.toString())
                );
            }
        });

        // Capture usage metadata
        response.usageMetadata().ifPresent(usage -> {
            int prompt = usage.promptTokenCount().orElse(0);
            int candidates = usage.candidatesTokenCount().orElse(0);
            int total = usage.totalTokenCount().orElse(0);
            tracer.setTokenCounts(span, prompt, candidates, total);
        });

        tracer.setRawOutput(span, response);
    }

    private String extractContentText(Content content) {
        if (content == null) {
            return "";
        }
        try {
            String text = content.text();
            return text != null ? text : "";
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * Traced wrapper for Chat sessions.
     */
    public static class TracedChat {
        private final Chat chat;
        private final FITracer tracer;
        private final String modelName;

        TracedChat(Chat chat, FITracer tracer, String modelName) {
            this.chat = chat;
            this.tracer = tracer;
            this.modelName = modelName;
        }

        /**
         * Sends a message with tracing.
         *
         * @param message the message to send
         * @return the response
         */
        public GenerateContentResponse sendMessage(String message) {
            Span span = tracer.startSpan("Google GenAI Chat Message", FISpanKind.LLM);

            try (Scope scope = span.makeCurrent()) {
                span.setAttribute(SemanticConventions.LLM_SYSTEM, "google-genai");
                span.setAttribute(SemanticConventions.LLM_PROVIDER, "google");
                span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);

                tracer.setInputValue(span, message);
                tracer.setInputMessages(span, Collections.singletonList(FITracer.message("user", message)));

                GenerateContentResponse response = chat.sendMessage(message);

                // Capture output
                try {
                    String text = response.text();
                    if (text != null) {
                        tracer.setOutputValue(span, text);
                        tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("model", text)));
                    }
                } catch (Exception e) {
                    // text() may throw if no text candidates
                }

                // Token usage
                response.usageMetadata().ifPresent(usage -> {
                    int prompt = usage.promptTokenCount().orElse(0);
                    int candidates = usage.candidatesTokenCount().orElse(0);
                    int total = usage.totalTokenCount().orElse(0);
                    tracer.setTokenCounts(span, prompt, candidates, total);
                });

                span.setStatus(StatusCode.OK);
                return response;

            } catch (Exception e) {
                tracer.setError(span, e);
                throw new RuntimeException("Google GenAI chat message failed", e);
            } finally {
                span.end();
            }
        }

        /**
         * Gets the underlying Chat.
         *
         * @return the wrapped Chat
         */
        public Chat unwrap() {
            return chat;
        }
    }
}
