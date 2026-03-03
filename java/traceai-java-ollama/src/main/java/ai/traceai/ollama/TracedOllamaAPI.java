package ai.traceai.ollama;

import ai.traceai.*;
import io.github.ollama4j.OllamaAPI;
import io.github.ollama4j.models.OllamaResult;
import io.github.ollama4j.models.chat.*;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Instrumentation wrapper for Ollama4j OllamaAPI.
 * Wraps the OllamaAPI to provide automatic tracing of all API calls.
 *
 * <p>Usage:</p>
 * <pre>
 * OllamaAPI ollama = new OllamaAPI("http://localhost:11434");
 * TracedOllamaAPI traced = new TracedOllamaAPI(ollama);
 *
 * OllamaResult result = traced.generate("llama3", "Hello!");
 * </pre>
 */
public class TracedOllamaAPI {

    private final OllamaAPI api;
    private final FITracer tracer;

    /**
     * Creates a new traced Ollama API with the given API and tracer.
     *
     * @param api    the OllamaAPI to wrap
     * @param tracer the FITracer for instrumentation
     */
    public TracedOllamaAPI(OllamaAPI api, FITracer tracer) {
        this.api = api;
        this.tracer = tracer;
    }

    /**
     * Creates a new traced Ollama API using the global TraceAI tracer.
     *
     * @param api the OllamaAPI to wrap
     */
    public TracedOllamaAPI(OllamaAPI api) {
        this(api, TraceAI.getTracer());
    }

    /**
     * Generates a completion with tracing.
     *
     * @param model  the model name
     * @param prompt the prompt
     * @return the generation result
     * @throws Exception if generation fails
     */
    public OllamaResult generate(String model, String prompt) throws Exception {
        Span span = tracer.startSpan("Ollama Generate", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "ollama");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "ollama");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);
            span.setAttribute(SemanticConventions.LLM_REQUEST_MODEL, model);

            // Capture input
            tracer.setInputValue(span, prompt);
            tracer.setInputMessages(span, Collections.singletonList(FITracer.message("user", prompt)));

            // Execute request
            OllamaResult result = api.generate(model, prompt, false, null);

            // Capture output
            if (result.getResponse() != null) {
                tracer.setOutputValue(span, result.getResponse());
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", result.getResponse())));
            }

            // Capture metrics if available
            if (result.getResponseTime() > 0) {
                span.setAttribute("ollama.response_time_ms", result.getResponseTime());
            }

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
     * Sends a chat request with tracing.
     *
     * @param model    the model name
     * @param messages the chat messages
     * @return the chat result
     * @throws Exception if chat fails
     */
    public OllamaChatResult chat(String model, List<OllamaChatMessage> messages) throws Exception {
        Span span = tracer.startSpan("Ollama Chat", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "ollama");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "ollama");
            span.setAttribute(SemanticConventions.LLM_MODEL_NAME, model);

            // Capture input messages
            List<Map<String, String>> inputMessages = new ArrayList<>();
            for (OllamaChatMessage msg : messages) {
                String role = msg.getRole().toString().toLowerCase();
                String content = msg.getContent();
                inputMessages.add(FITracer.message(role, content));
            }
            tracer.setInputMessages(span, inputMessages);

            tracer.setRawInput(span, messages);

            // Execute request using the direct chat(model, messages) overload
            OllamaChatResult result = api.chat(model, messages);

            // Capture output
            if (result.getResponse() != null) {
                tracer.setOutputValue(span, result.getResponse());
                tracer.setOutputMessages(span, Collections.singletonList(FITracer.message("assistant", result.getResponse())));
            }

            // Capture metrics
            if (result.getResponseTime() > 0) {
                span.setAttribute("ollama.response_time_ms", result.getResponseTime());
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
     * Creates embeddings with tracing.
     *
     * @param model the model name
     * @param text  the text to embed
     * @return the embedding vector as a list of doubles
     * @throws Exception if embedding fails
     */
    public List<Double> embed(String model, String text) throws Exception {
        Span span = tracer.startSpan("Ollama Embed", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "ollama");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "ollama");
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, model);

            // Capture input
            tracer.setInputValue(span, text);

            // Execute request
            List<Double> embedding = api.generateEmbeddings(model, text);

            // Capture output
            if (embedding != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                    (long) embedding.size());
                span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, 1L);
            }

            span.setStatus(StatusCode.OK);
            return embedding;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Creates embeddings for multiple texts with tracing.
     *
     * @param model the model name
     * @param texts the texts to embed
     * @return the embedding vectors
     * @throws Exception if embedding fails
     */
    public List<List<Double>> embedBatch(String model, List<String> texts) throws Exception {
        Span span = tracer.startSpan("Ollama Embed Batch", FISpanKind.EMBEDDING);

        try (Scope scope = span.makeCurrent()) {
            // Set system attributes
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "ollama");
            span.setAttribute(SemanticConventions.LLM_PROVIDER, "ollama");
            span.setAttribute(SemanticConventions.EMBEDDING_MODEL_NAME, model);
            span.setAttribute(SemanticConventions.EMBEDDING_VECTOR_COUNT, (long) texts.size());

            // Capture input
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

            // Execute requests
            List<List<Double>> responses = new ArrayList<>();
            for (String text : texts) {
                responses.add(api.generateEmbeddings(model, text));
            }

            // Capture output
            if (!responses.isEmpty() && responses.get(0) != null) {
                span.setAttribute(SemanticConventions.EMBEDDING_DIMENSIONS,
                    (long) responses.get(0).size());
            }

            span.setStatus(StatusCode.OK);
            return responses;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Lists available models with tracing.
     *
     * @return list of model names
     * @throws Exception if listing fails
     */
    public List<String> listModels() throws Exception {
        Span span = tracer.startSpan("Ollama List Models", FISpanKind.LLM);

        try (Scope scope = span.makeCurrent()) {
            span.setAttribute(SemanticConventions.LLM_SYSTEM, "ollama");

            var models = api.listModels();
            var modelNames = models.stream()
                .map(m -> m.getModelName())
                .toList();

            span.setAttribute("ollama.models_count", (long) modelNames.size());
            span.setStatus(StatusCode.OK);
            return modelNames;

        } catch (Exception e) {
            tracer.setError(span, e);
            throw e;
        } finally {
            span.end();
        }
    }

    /**
     * Gets the underlying OllamaAPI.
     *
     * @return the wrapped OllamaAPI
     */
    public OllamaAPI unwrap() {
        return api;
    }
}
