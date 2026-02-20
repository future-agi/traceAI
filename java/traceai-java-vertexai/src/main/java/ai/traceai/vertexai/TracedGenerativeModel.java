package ai.traceai.vertexai;

import ai.traceai.*;
import com.google.cloud.vertexai.api.*;
import com.google.cloud.vertexai.generativeai.GenerativeModel;
import com.google.cloud.vertexai.generativeai.ResponseStream;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Google Cloud Vertex AI GenerativeModel.
 * Wraps the GenerativeModel to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * VertexAI vertexAI = new VertexAI(projectId, location);
 * GenerativeModel model = new GenerativeModel("gemini-1.5-pro", vertexAI);
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
     * @param model  the GenerativeModel to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedGenerativeModel(GenerativeModel model, FITracer tracer) {
        this.model = model;
        this.tracer = tracer;
        this.modelName = model.getModelName();
    }

    /**
     * Creates a new traced generative model using the global TraceAI tracer.
     *
     * @param model the GenerativeModel to wrap
     */
    public TracedGenerativeModel(GenerativeModel model) {
        this(model, TraceAI.getTracer());
    }

    /**
     * Generates content with tracing.
     *
     * @param text the input text
     * @return the generate content response
     * @throws IOException if an I/O error occurs
     */
    public GenerateContentResponse generateContent(String text) throws IOException {
        Span span = tracer.startSpan("Vertex AI Generate Content", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "vertexai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "google");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, modelName);

            // Capture input
            tracer.setInputValue(span, text);
            tracer.setInputMessages(span, Collections.singletonList(FITracer.message("user", text)));

            // Execute request
            GenerateContentResponse response = model.generateContent(text);

            // Capture output
            captureResponse(span, response);

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
     * Generates content with tracing using Content objects.
     *
     * @param contents the input contents
     * @return the generate content response
     * @throws IOException if an I/O error occurs
     */
    public GenerateContentResponse generateContent(List<Content> contents) throws IOException {
        Span span = tracer.startSpan("Vertex AI Generate Content", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "vertexai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "google");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);

            // Capture input messages
            List<Map<String, String>> inputMessages = new ArrayList<>();
            for (Content content : contents) {
                String role = content.getRole();
                String contentText = extractContentText(content);
                inputMessages.add(FITracer.message(role != null ? role : "user", contentText));
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
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Generates content with streaming and tracing.
     *
     * @param text the input text
     * @return a stream of generate content responses
     * @throws IOException if an I/O error occurs
     */
    public ResponseStream<GenerateContentResponse> generateContentStream(String text) throws IOException {
        Span span = tracer.startSpan("Vertex AI Generate Content (Stream)", FISpanKind.LLM);

        try {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "vertexai");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "google");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);

            // Capture input
            tracer.setInputValue(span, text);
            tracer.setInputMessages(span, Collections.singletonList(FITracer.message("user", text)));

            // Get the stream
            ResponseStream<GenerateContentResponse> stream = model.generateContentStream(text);

            // Note: Stream wrapping requires custom implementation
            // For now, we return the original stream and end the span
            // A complete implementation would wrap the stream to capture chunks
            span.setStatus(StatusCode.OK);
            span.end();

            return stream;

        } catch (Exception e) {
            tracer.setError(span, e);
            span.end();
            throw e;
        }
    }

    /**
     * Counts tokens with tracing.
     *
     * @param text the text to count tokens for
     * @return the count tokens response
     * @throws IOException if an I/O error occurs
     */
    public CountTokensResponse countTokens(String text) throws IOException {
        Span span = tracer.startSpan("Vertex AI Count Tokens", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "vertexai");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, modelName);

            tracer.setInputValue(span, text);

            CountTokensResponse response = model.countTokens(text);

            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, (long) response.getTotalTokens());
            span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_PROMPT, (long) response.getTotalTokens());

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
        List<Candidate> candidates = response.getCandidatesList();
        if (candidates != null && !candidates.isEmpty()) {
            Candidate firstCandidate = candidates.get(0);

            // Capture content
            if (firstCandidate.hasContent()) {
                String text = extractContentText(firstCandidate.getContent());
                tracer.setOutputValue(span, text);
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("model", text)));
            }

            // Capture finish reason
            if (firstCandidate.hasFinishReason()) {
                span.setAttribute(SemanticConventions.LLM_RESPONSE_FINISH_REASON,
                    firstCandidate.getFinishReason().name());
            }
        }

        // Capture usage metadata
        if (response.hasUsageMetadata()) {
            UsageMetadata usage = response.getUsageMetadata();
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
        if (content == null || content.getPartsList() == null) {
            return "";
        }

        StringBuilder sb = new StringBuilder();
        for (Part part : content.getPartsList()) {
            if (part.hasText()) {
                sb.append(part.getText());
            }
        }
        return sb.toString();
    }
}
