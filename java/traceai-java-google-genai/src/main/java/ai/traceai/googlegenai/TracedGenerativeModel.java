package ai.traceai.googlegenai;

import ai.traceai.*;
import com.google.ai.client.generativeai.GenerativeModel;
import com.google.ai.client.generativeai.type.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Google Generative AI (Gemini) client.
 * Wraps the GenerativeModel to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * GenerativeModel model = new GenerativeModel("gemini-1.5-pro", apiKey);
 * TracedGenerativeModel traced = new TracedGenerativeModel(model);
 *
 * GenerateContentResponse response = traced.generateContent("Hello!");
 * </pre>
 */
public class TracedGenerativeModel {

    private final GenerativeModel model;
    private final FITracer tracer;
    private final String modelName;

    /**
     * Creates a new traced generative model with the given model and tracer.
     *
     * @param model     the GenerativeModel to wrap
     * @param tracer    the FITracer for instrumentation
     * @param modelName the model name for tracing
     */
    public TracedGenerativeModel(GenerativeModel model, FITracer tracer, String modelName) {
        this.model = model;
        this.tracer = tracer;
        this.modelName = modelName;
    }

    /**
     * Creates a new traced generative model using the global TraceAI tracer.
     *
     * @param model     the GenerativeModel to wrap
     * @param modelName the model name for tracing
     */
    public TracedGenerativeModel(GenerativeModel model, String modelName) {
        this(model, TraceAI.getTracer(), modelName);
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
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "google-genai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "google");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelName);

            // Capture input
            tracer.setInputValue(span, prompt);
            tracer.setInputMessages(span, Collections.singletonList(FITracer.message("user", prompt)));

            // Execute request
            GenerateContentResponse response = model.generateContent(prompt);

            // Capture output
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
    public GenerateContentResponse generateContent(Content... contents) {
        Span span = tracer.startSpan("Google GenAI Generate Content", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "google-genai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "google");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);

            // Capture input messages
            List<Map<String, String>> inputMessages = new ArrayList<>();
            for (Content content : contents) {
                String role = content.getRole() != null ? content.getRole() : "user";
                String text = extractContentText(content);
                inputMessages.add(FITracer.message(role, text));
            }
            tracer.setInputMessages(span, inputMessages);

            tracer.setRawInput(span, contents);

            // Execute request
            GenerateContentResponse response = model.generateContent(contents);

            // Capture output
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

            CountTokensResponse response = model.countTokens(prompt);

            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, (long) response.getTotalTokens());

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
        return new TracedChat(model.startChat(), tracer, modelName);
    }

    /**
     * Starts a chat session with history and tracing support.
     *
     * @param history the chat history
     * @return a traced chat session
     */
    public TracedChat startChat(List<Content> history) {
        return new TracedChat(model.startChat(history), tracer, modelName);
    }

    /**
     * Gets the underlying GenerativeModel.
     *
     * @return the wrapped GenerativeModel
     */
    public GenerativeModel unwrap() {
        return model;
    }

    private void captureResponse(Span span, GenerateContentResponse response) {
        if (response == null) return;

        // Capture candidates
        List<Candidate> candidates = response.getCandidates();
        if (candidates != null && !candidates.isEmpty()) {
            Candidate firstCandidate = candidates.get(0);

            // Capture content
            if (firstCandidate.getContent() != null) {
                String text = extractContentText(firstCandidate.getContent());
                tracer.setOutputValue(span, text);
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("model", text)));
            }

            // Capture finish reason
            if (firstCandidate.getFinishReason() != null) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                    firstCandidate.getFinishReason().name());
            }
        }

        // Capture usage metadata
        UsageMetadata usage = response.getUsageMetadata();
        if (usage != null) {
            tracer.setTokenCounts(
                span,
                usage.getPromptTokenCount(),
                usage.getCandidatesTokenCount(),
                usage.getTotalTokenCount()
            );
        }

        tracer.setRawOutput(span, response);
    }

    private String extractContentText(Content content) {
        if (content == null || content.getParts() == null) {
            return "";
        }

        StringBuilder sb = new StringBuilder();
        for (Part part : content.getParts()) {
            if (part instanceof TextPart) {
                sb.append(((TextPart) part).getText());
            }
        }
        return sb.toString();
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
                if (response.getCandidates() != null && !response.getCandidates().isEmpty()) {
                    Candidate candidate = response.getCandidates().get(0);
                    if (candidate.getContent() != null) {
                        StringBuilder sb = new StringBuilder();
                        for (Part part : candidate.getContent().getParts()) {
                            if (part instanceof TextPart) {
                                sb.append(((TextPart) part).getText());
                            }
                        }
                        String text = sb.toString();
                        tracer.setOutputValue(span, text);
                        tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("model", text)));
                    }
                }

                // Token usage
                UsageMetadata usage = response.getUsageMetadata();
                if (usage != null) {
                    tracer.setTokenCounts(
                        span,
                        usage.getPromptTokenCount(),
                        usage.getCandidatesTokenCount(),
                        usage.getTotalTokenCount()
                    );
                }

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
