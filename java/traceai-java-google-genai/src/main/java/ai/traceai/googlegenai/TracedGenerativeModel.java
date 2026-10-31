package ai.traceai.googlegenai;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceAI;
import com.google.genai.Chat;
import com.google.genai.Client;
import com.google.genai.ResponseStream;
import com.google.genai.types.Content;
import com.google.genai.types.CountTokensConfig;
import com.google.genai.types.CountTokensResponse;
import com.google.genai.types.EmbedContentConfig;
import com.google.genai.types.EmbedContentResponse;
import com.google.genai.types.GenerateContentConfig;
import com.google.genai.types.GenerateContentResponse;
import io.opentelemetry.context.Scope;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.function.BiConsumer;
import java.util.function.Supplier;

/**
 * Traces calls made through this facade on Google GenAI SDK 1.1.0 and later.
 * Supports generation (sync, async, streaming), embeddings, token counting and
 * synchronous chat. The supplied client retains its configuration and ownership.
 * Direct calls to the original client or {@link #unwrap()} are not instrumented.
 *
 * <p>Configure tools, system instructions and structured outputs with the SDK's
 * GenerateContentConfig. Close streams with try-with-resources, including early exits.
 * One span represents one logical SDK call, not each automatic function-call round.</p>
 */
public class TracedGenerativeModel {
    private final Client client;
    private final FITracer tracer;
    private final String modelName;

    public TracedGenerativeModel(Client client, FITracer tracer, String modelName) {
        this.client = Objects.requireNonNull(client, "client");
        this.tracer = Objects.requireNonNull(tracer, "tracer");
        this.modelName = Objects.requireNonNull(modelName, "modelName");
    }

    public TracedGenerativeModel(Client client, String modelName) {
        this(client, TraceAI.getTracer(), modelName);
    }

    public GenerateContentResponse generateContent(String prompt) {
        return generateContent(prompt, null);
    }

    public GenerateContentResponse generateContent(String prompt, GenerateContentConfig config) {
        return sync(generation(prompt, config), () -> client.models.generateContent(modelName, prompt, config),
            (op, response) -> op.response(response, false));
    }

    public GenerateContentResponse generateContent(List<Content> contents) {
        return generateContent(contents, null);
    }

    public GenerateContentResponse generateContent(List<Content> contents, GenerateContentConfig config) {
        return sync(generation(contents, config), () -> client.models.generateContent(modelName, contents, config),
            (op, response) -> op.response(response, false));
    }

    public CompletableFuture<GenerateContentResponse> generateContentAsync(String prompt, GenerateContentConfig config) {
        return async(generation(prompt, config), () -> client.async.models.generateContent(modelName, prompt, config),
            (op, response) -> op.response(response, false));
    }

    public CompletableFuture<GenerateContentResponse> generateContentAsync(List<Content> contents, GenerateContentConfig config) {
        return async(generation(contents, config), () -> client.async.models.generateContent(modelName, contents, config),
            (op, response) -> op.response(response, false));
    }

    public TracedResponseStream generateContentStream(String prompt, GenerateContentConfig config) {
        return stream(generation(prompt, config), () -> client.models.generateContentStream(modelName, prompt, config));
    }

    public TracedResponseStream generateContentStream(List<Content> contents, GenerateContentConfig config) {
        return stream(generation(contents, config), () -> client.models.generateContentStream(modelName, contents, config));
    }

    public CompletableFuture<TracedResponseStream> generateContentStreamAsync(String prompt, GenerateContentConfig config) {
        return asyncStream(generation(prompt, config), () -> client.async.models.generateContentStream(modelName, prompt, config));
    }

    public CompletableFuture<TracedResponseStream> generateContentStreamAsync(List<Content> contents, GenerateContentConfig config) {
        return asyncStream(generation(contents, config), () -> client.async.models.generateContentStream(modelName, contents, config));
    }

    public EmbedContentResponse embedContent(String text, EmbedContentConfig config) {
        return sync(embedding(text), () -> client.models.embedContent(modelName, text, config), GenAiOperation::embeddings);
    }

    public EmbedContentResponse embedContent(List<String> texts, EmbedContentConfig config) {
        return sync(embedding(texts), () -> client.models.embedContent(modelName, texts, config), GenAiOperation::embeddings);
    }

    public CompletableFuture<EmbedContentResponse> embedContentAsync(String text, EmbedContentConfig config) {
        return async(embedding(text), () -> client.async.models.embedContent(modelName, text, config), GenAiOperation::embeddings);
    }

    public CompletableFuture<EmbedContentResponse> embedContentAsync(List<String> texts, EmbedContentConfig config) {
        return async(embedding(texts), () -> client.async.models.embedContent(modelName, texts, config), GenAiOperation::embeddings);
    }

    public CountTokensResponse countTokens(String prompt) {
        return countTokens(prompt, null);
    }

    public CountTokensResponse countTokens(String prompt, CountTokensConfig config) {
        return sync(new GenAiOperation(tracer, "Count Tokens", modelName, FISpanKind.LLM, prompt, null),
            () -> client.models.countTokens(modelName, prompt, config), (op, response) ->
                response.totalTokens().ifPresent(total -> op.span.setAttribute(SemanticConventions.LLM_TOKEN_COUNT_TOTAL, total.longValue())));
    }

    public TracedChat startChat() {
        return startChat(null);
    }

    public TracedChat startChat(GenerateContentConfig config) {
        return new TracedChat(client.chats.create(modelName, config), tracer, modelName, config);
    }

    /** Returns the original, uninstrumented client. Calls through it do not create facade spans. */
    public Client unwrap() {
        return client;
    }

    public String getModelName() {
        return modelName;
    }

    private GenAiOperation generation(Object input, GenerateContentConfig config) {
        return new GenAiOperation(tracer, "Generate Content", modelName, FISpanKind.LLM, input, config);
    }

    private GenAiOperation embedding(Object input) {
        return new GenAiOperation(tracer, "Embed Content", modelName, FISpanKind.EMBEDDING, input, null);
    }

    private static <T> T sync(GenAiOperation op, Supplier<T> call, BiConsumer<GenAiOperation, T> capture) {
        try (Scope ignored = op.context.makeCurrent()) {
            T response = call.get();
            GenAiOperation.safely(() -> capture.accept(op, response));
            op.finish(null, false, false);
            return response;
        } catch (RuntimeException | Error error) {
            op.finish(error, false, false);
            throw error;
        }
    }

    private static TracedResponseStream stream(GenAiOperation op, Supplier<ResponseStream<GenerateContentResponse>> call) {
        try (Scope ignored = op.context.makeCurrent()) {
            return new TracedResponseStream(call.get(), op);
        } catch (RuntimeException | Error error) {
            op.finish(error, true, false);
            throw error;
        }
    }

    private static <T> CompletableFuture<T> async(GenAiOperation op, Supplier<CompletableFuture<T>> call,
                                                 BiConsumer<GenAiOperation, T> capture) {
        CompletableFuture<T> source;
        try (Scope ignored = op.context.makeCurrent()) {
            source = call.get();
        } catch (RuntimeException | Error error) {
            op.finish(error, false, false);
            throw error;
        }
        CompletableFuture<T> result = new CompletableFuture<>();
        result.whenComplete((value, error) -> {
            if (result.isCancelled()) {
                op.finish(null, false, true);
                source.cancel(true);
            }
        });
        source.whenComplete((value, error) -> {
            try (Scope ignored = op.context.makeCurrent()) {
                if (result.isCancelled()) return;
                if (error == null) GenAiOperation.safely(() -> capture.accept(op, value));
                op.finish(unwrapError(error), false, false);
                if (error == null) result.complete(value);
                else result.completeExceptionally(unwrapError(error));
            }
        });
        return result;
    }

    static CompletableFuture<TracedResponseStream> asyncStream(GenAiOperation op,
        Supplier<CompletableFuture<ResponseStream<GenerateContentResponse>>> call) {
        CompletableFuture<ResponseStream<GenerateContentResponse>> source;
        try (Scope ignored = op.context.makeCurrent()) {
            source = call.get();
        } catch (RuntimeException | Error error) {
            op.finish(error, true, false);
            throw error;
        }
        CompletableFuture<TracedResponseStream> result = new CompletableFuture<>();
        result.whenComplete((value, error) -> {
            if (result.isCancelled()) {
                // Do not cancel the upstream future: older SDKs discard a late stream
                // on cancellation without closing it. Close it when it eventually arrives.
                op.finish(null, true, true);
            }
        });
        source.whenComplete((value, error) -> {
            try (Scope ignored = op.context.makeCurrent()) {
                if (error != null) {
                    op.finish(unwrapError(error), true, false);
                    result.completeExceptionally(unwrapError(error));
                } else {
                    TracedResponseStream wrapped = new TracedResponseStream(value, op);
                    if (!result.complete(wrapped)) wrapped.close();
                }
            } catch (RuntimeException | Error failure) {
                op.finish(failure, true, false);
                result.completeExceptionally(failure);
            }
        });
        return result;
    }

    private static Throwable unwrapError(Throwable error) {
        return error instanceof CompletionException && error.getCause() != null ? error.getCause() : error;
    }

    /** Stateful synchronous chat, including streaming and per-turn configuration. */
    public static class TracedChat {
        private final Chat chat;
        private final FITracer tracer;
        private final String modelName;
        private final GenerateContentConfig config;

        TracedChat(Chat chat, FITracer tracer, String modelName, GenerateContentConfig config) {
            this.chat = chat;
            this.tracer = tracer;
            this.modelName = modelName;
            this.config = config;
        }

        public GenerateContentResponse sendMessage(String message) {
            return sendMessage(message, null);
        }

        public GenerateContentResponse sendMessage(String message, GenerateContentConfig config) {
            return sync(operation(message, config), () -> chat.sendMessage(message, config), (op, response) -> op.response(response, false));
        }

        public GenerateContentResponse sendMessage(List<Content> contents, GenerateContentConfig config) {
            return sync(operation(contents, config), () -> chat.sendMessage(contents, config), (op, response) -> op.response(response, false));
        }

        public TracedResponseStream sendMessageStream(String message, GenerateContentConfig config) {
            return stream(operation(message, config), () -> chat.sendMessageStream(message, config));
        }

        public TracedResponseStream sendMessageStream(List<Content> contents, GenerateContentConfig config) {
            return stream(operation(contents, config), () -> chat.sendMessageStream(contents, config));
        }

        private GenAiOperation operation(Object input, GenerateContentConfig config) {
            return new GenAiOperation(tracer, "Chat Message", modelName, FISpanKind.LLM, input,
                config != null ? config : this.config);
        }

        /** Returns the original, uninstrumented chat. */
        public Chat unwrap() {
            return chat;
        }
    }
}
