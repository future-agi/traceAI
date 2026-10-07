package local.th7477;

import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.metadata.ChatResponseMetadata;
import org.springframework.ai.chat.metadata.Usage;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.document.Document;
import org.springframework.ai.embedding.Embedding;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.embedding.EmbeddingRequest;
import org.springframework.ai.embedding.EmbeddingResponse;
import org.springframework.ai.embedding.EmbeddingResponseMetadata;
import reactor.core.publisher.Flux;

import java.util.List;
import java.util.function.Function;

/** Spring AI objects shared by both matrix cells. Only APIs present in 1.1.0 and 1.1.8. */
final class SpanTestSupport {
    private SpanTestSupport() {}

    static ChatResponse response(String text, Usage usage, String model) {
        ChatResponseMetadata.Builder metadata = ChatResponseMetadata.builder();
        if (usage != null) metadata.usage(usage);
        if (model != null) metadata.model(model);
        return new ChatResponse(List.of(new Generation(new AssistantMessage(text))), metadata.build());
    }

    static Usage usage(int prompt, int completion, int total) {
        return new Usage() {
            @Override public Integer getPromptTokens() { return prompt; }
            @Override public Integer getCompletionTokens() { return completion; }
            @Override public Integer getTotalTokens() { return total; }
            @Override public Object getNativeUsage() { return "matrix"; }
        };
    }

    static ChatModel syncModel(Function<Prompt, ChatResponse> call) {
        return new ChatModel() {
            @Override public ChatResponse call(Prompt prompt) { return call.apply(prompt); }
            @Override public Flux<ChatResponse> stream(Prompt prompt) { return Flux.just(response("streamed-" + prompt.getContents(), null, null)); }
            @Override public ChatOptions getDefaultOptions() { return null; }
        };
    }

    static final class FixedEmbeddingModel implements EmbeddingModel {
        private final EmbeddingResponse response = new EmbeddingResponse(
            List.of(new Embedding(new float[]{1f, 2f, 3f}, 0), new Embedding(new float[]{4f, 5f, 6f}, 1)),
            new EmbeddingResponseMetadata("m", null));
        @Override public EmbeddingResponse call(EmbeddingRequest request) { return response; }
        @Override public float[] embed(Document document) { return new float[]{1f, 2f, 3f}; }
        @Override public List<float[]> embed(List<String> texts) { return List.of(new float[]{1f, 2f, 3f}, new float[]{4f, 5f, 6f}); }
    }
}
