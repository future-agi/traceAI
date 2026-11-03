package ai.traceai.spring.compatibility;

import io.micrometer.observation.ObservationRegistry;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.model.tool.DefaultToolCallingManager;
import org.springframework.ai.openai.OpenAiChatModel;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.openai.OpenAiEmbeddingModel;
import org.springframework.ai.openai.OpenAiEmbeddingOptions;

final class Models {
    static ChatModel chat(String baseUrl, ObservationRegistry registry) {
        return OpenAiChatModel.builder()
            .options(OpenAiChatOptions.builder().baseUrl(baseUrl + "/v1").apiKey("synthetic-key")
                .model("synthetic-model").maxRetries(0).streamUsage(true).build())
            .observationRegistry(registry).build();
    }
    static EmbeddingModel embedding(String baseUrl, ObservationRegistry registry) {
        return OpenAiEmbeddingModel.builder()
            .options(OpenAiEmbeddingOptions.builder().baseUrl(baseUrl + "/v1").apiKey("synthetic-key")
                .model("synthetic-embedding").maxRetries(0)
                .encodingFormat(OpenAiEmbeddingOptions.EncodingFormat.FLOAT).build())
            .observationRegistry(registry).build();
    }
    static org.springframework.ai.chat.model.ChatResponse toolChat(String baseUrl, ObservationRegistry registry, org.springframework.ai.chat.prompt.Prompt prompt) {
        // Spring AI 2 moves the tool loop out of ChatModel; exercise its real tool manager.
        var model = chat(baseUrl, registry);
        var first = model.call(prompt);
        var tools = DefaultToolCallingManager.builder().observationRegistry(registry).build();
        var result = tools.executeToolCalls(prompt, first);
        return model.call(new org.springframework.ai.chat.prompt.Prompt(result.conversationHistory()));
    }

}
