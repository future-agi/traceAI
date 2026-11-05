package ai.traceai.spring.compatibility;

import io.micrometer.observation.ObservationRegistry;
import ai.traceai.spring.SpringAIToolExecutionExceptionProcessor;
import org.springframework.ai.model.tool.ToolCallingManager;
import org.springframework.ai.tool.execution.DefaultToolExecutionExceptionProcessor;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.model.tool.DefaultToolCallingManager;
import org.springframework.ai.openai.OpenAiChatModel;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.openai.OpenAiEmbeddingModel;
import org.springframework.ai.openai.OpenAiEmbeddingOptions;

final class Models {
    static org.springframework.ai.chat.prompt.ChatOptions chatOptions(ChatModel model) {
        return model.getOptions();
    }

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
        var processor = new SpringAIToolExecutionExceptionProcessor(registry,
            DefaultToolExecutionExceptionProcessor.builder().build());
        var manager = DefaultToolCallingManager.builder().observationRegistry(registry)
            .toolExecutionExceptionProcessor(processor).build();
        return toolChat(baseUrl, registry, prompt, manager);
    }

    static org.springframework.ai.chat.model.ChatResponse toolChat(String baseUrl, ObservationRegistry registry,
            org.springframework.ai.chat.prompt.Prompt prompt, ToolCallingManager manager) {
        // Spring AI 2 moves the tool loop out of ChatModel; exercise its real tool manager.
        var model = chat(baseUrl, registry);
        var first = model.call(prompt);
        var result = manager.executeToolCalls(prompt, first);
        return model.call(new org.springframework.ai.chat.prompt.Prompt(result.conversationHistory()));
    }

}
