package ai.traceai.examples;

import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import ai.traceai.langchain4j.LangChain4jListener;
import ai.traceai.langchain4j.TracedAiServices;
import dev.langchain4j.model.chat.ChatModel;
import dev.langchain4j.model.openai.OpenAiChatModel;
import dev.langchain4j.service.AiServices;

import java.util.List;

/**
 * Run with FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME, and OPENAI_API_KEY set:
 * {@code mvn exec:java}. Only running main calls a provider; compiling does not.
 */
public class LangChain4jExample {
    interface Assistant { String chat(String message); }

    public static void main(String[] args) {
        TraceAI.init(TraceConfig.builder()
                .baseUrl(System.getenv("FI_BASE_URL"))
                .apiKey(System.getenv("FI_API_KEY"))
                .secretKey(System.getenv("FI_SECRET_KEY"))
                .projectName(System.getenv("FI_PROJECT_NAME"))
                .hideInputs(true).hideOutputs(true).hideErrorDetails(true)
                .build());
        try {
            // Register the official callback once; no model wrapper or Java agent is needed.
            ChatModel model = OpenAiChatModel.builder()
                    .apiKey(System.getenv("OPENAI_API_KEY"))
                    .modelName("gpt-4o-mini")
                    .listeners(List.of(new LangChain4jListener("openai")))
                    .build();
            Assistant assistant = AiServices.builder(Assistant.class).chatModel(model).build();
            // Optional synchronous service parent. The model listener records its child calls.
            Assistant traced = TracedAiServices.create(Assistant.class, assistant);
            System.out.println(traced.chat("Explain what a Java interface is in one sentence."));
        } finally {
            TraceAI.shutdown();
        }
    }
}
