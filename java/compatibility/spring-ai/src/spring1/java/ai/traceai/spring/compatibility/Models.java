package ai.traceai.spring.compatibility;

import io.micrometer.observation.ObservationRegistry;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.document.MetadataMode;
import org.springframework.ai.model.tool.DefaultToolCallingManager;
import org.springframework.ai.openai.OpenAiChatModel;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.openai.OpenAiEmbeddingModel;
import org.springframework.ai.openai.OpenAiEmbeddingOptions;
import org.springframework.ai.openai.api.OpenAiApi;
import org.springframework.retry.support.RetryTemplate;

final class Models {
    static ChatModel chat(String baseUrl, ObservationRegistry registry) {
        return OpenAiChatModel.builder()
            .openAiApi(api(baseUrl))
            .defaultOptions(OpenAiChatOptions.builder().model("synthetic-model").streamUsage(true).build())
            .retryTemplate(RetryTemplate.builder().maxAttempts(1).build())
            .toolCallingManager(DefaultToolCallingManager.builder().observationRegistry(registry).build())
            .observationRegistry(registry).build();
    }
    static EmbeddingModel embedding(String baseUrl, ObservationRegistry registry) {
        return new OpenAiEmbeddingModel(api(baseUrl), MetadataMode.EMBED,
            OpenAiEmbeddingOptions.builder().model("synthetic-embedding").build(),
            RetryTemplate.builder().maxAttempts(1).build(), registry);
    }
    private static OpenAiApi api(String baseUrl) {
        return OpenAiApi.builder().baseUrl(baseUrl).apiKey("synthetic-key").build();
    }
    static org.springframework.ai.chat.model.ChatResponse toolChat(String baseUrl, ObservationRegistry registry, org.springframework.ai.chat.prompt.Prompt prompt) {
        return chat(baseUrl, registry).call(prompt);
    }

}
