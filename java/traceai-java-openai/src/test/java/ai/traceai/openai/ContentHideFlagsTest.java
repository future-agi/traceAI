package ai.traceai.openai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.openai.client.OpenAIClient;
import com.openai.core.ObjectMappers;
import com.openai.core.http.StreamResponse;
import com.openai.models.ChatCompletion;
import com.openai.models.ChatCompletionChunk;
import com.openai.models.ChatCompletionCreateParams;
import com.openai.models.ChatCompletionMessageParam;
import com.openai.models.ChatCompletionUserMessageParam;
import com.openai.models.CreateEmbeddingResponse;
import com.openai.models.EmbeddingCreateParams;
import com.openai.services.blocking.ChatService;
import com.openai.services.blocking.EmbeddingService;
import com.openai.services.blocking.chat.CompletionService;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * Prompt, response and embedding text must not be exported under the hide flags on the chat, streaming and
 * embedding paths. Offline: the SDK service objects are stubs, no transport or credentials.
 */
class ContentHideFlagsTest {
    private static final String IN = "SENTINEL-INPUT-PROMPT-DO-NOT-EXPORT";
    private static final String OUT = "SENTINEL-OUTPUT-RESPONSE-DO-NOT-EXPORT";

    private static ChatCompletionCreateParams chatParams() {
        return ChatCompletionCreateParams.builder()
            .model("gpt-4o-mini")
            .addMessage(ChatCompletionMessageParam.ofChatCompletionUserMessageParam(
                ChatCompletionUserMessageParam.builder()
                    .content(ChatCompletionUserMessageParam.Content.ofTextContent(IN)).build()))
            .build();
    }

    private static List<SpanData> run(TraceConfig config, Consumer<TracedOpenAIClient> call) throws Exception {
        InMemorySpanExporter exporter = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        try {
            ChatCompletion completion = ObjectMappers.jsonMapper().readValue(
                "{\"id\":\"c1\",\"choices\":[{\"finish_reason\":\"stop\",\"index\":0,\"logprobs\":null,"
                    + "\"message\":{\"content\":\"" + OUT + "\",\"refusal\":null,\"role\":\"assistant\"}}],"
                    + "\"created\":1,\"model\":\"gpt-4o-mini\",\"object\":\"chat.completion\"}",
                ChatCompletion.class);
            ChatCompletionChunk chunk = ObjectMappers.jsonMapper().readValue(
                "{\"id\":\"c1\",\"choices\":[{\"delta\":{\"content\":\"" + OUT + "\"},\"index\":0,"
                    + "\"finish_reason\":\"stop\"}],\"created\":1,\"model\":\"gpt-4o-mini\","
                    + "\"object\":\"chat.completion.chunk\"}",
                ChatCompletionChunk.class);
            CreateEmbeddingResponse embedding = ObjectMappers.jsonMapper().readValue(
                "{\"data\":[{\"embedding\":[0.1,0.2],\"index\":0,\"object\":\"embedding\"}],"
                    + "\"model\":\"text-embedding-3-small\",\"object\":\"list\","
                    + "\"usage\":{\"prompt_tokens\":1,\"total_tokens\":1}}",
                CreateEmbeddingResponse.class);

            @SuppressWarnings("unchecked")
            StreamResponse<ChatCompletionChunk> stream = mock(StreamResponse.class);
            when(stream.stream()).thenReturn(Stream.of(chunk));

            OpenAIClient client = mock(OpenAIClient.class);
            ChatService chat = mock(ChatService.class);
            CompletionService completions = mock(CompletionService.class);
            EmbeddingService embeddings = mock(EmbeddingService.class);
            when(client.chat()).thenReturn(chat);
            when(chat.completions()).thenReturn(completions);
            when(completions.create(any(ChatCompletionCreateParams.class))).thenReturn(completion);
            when(completions.createStreaming(any(ChatCompletionCreateParams.class))).thenReturn(stream);
            when(client.embeddings()).thenReturn(embeddings);
            when(embeddings.create(any(EmbeddingCreateParams.class))).thenReturn(embedding);

            call.accept(new TracedOpenAIClient(client, new FITracer(provider.get("t"), config)));
            return exporter.getFinishedSpanItems();
        } finally {
            provider.close();
        }
    }

    private static void invoke(TracedOpenAIClient c, String path) {
        try {
            switch (path) {
                case "chat" -> c.createChatCompletion(chatParams());
                case "stream" -> c.streamChatCompletion(chatParams());
                default -> c.createEmbedding(EmbeddingCreateParams.builder()
                    .model("text-embedding-3-small").input(IN).build());
            }
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static List<String> leakedKeys(List<SpanData> spans, String secret) {
        List<String> leaked = new ArrayList<>();
        for (SpanData span : spans) {
            span.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(secret)) leaked.add(k.getKey());
            });
            span.getEvents().forEach(e -> e.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(secret)) leaked.add("event:" + k.getKey());
            }));
        }
        return leaked;
    }

    @Test
    void controlExportsContentWhenNothingIsHidden() throws Exception {
        List<SpanData> chat = run(TraceConfig.builder().build(), c -> invoke(c, "chat"));
        assertThat(leakedKeys(chat, IN)).isNotEmpty();
        assertThat(leakedKeys(chat, OUT)).isNotEmpty();
        List<SpanData> emb = run(TraceConfig.builder().build(), c -> invoke(c, "embedding"));
        assertThat(leakedKeys(emb, IN)).isNotEmpty();
    }

    @ParameterizedTest(name = "{0} on {1}")
    @CsvSource({
        "hideInputs,chat", "hideInputs,stream", "hideInputs,embedding",
        "hideInputMessages,chat", "hideInputMessages,stream", "hideInputMessages,embedding",
        "hideOutputs,chat", "hideOutputs,stream",
        "hideOutputMessages,chat", "hideOutputMessages,stream"})
    void hideFlagKeepsContentOutOfEverySpan(String flag, String path) throws Exception {
        TraceConfig.Builder b = TraceConfig.builder();
        switch (flag) {
            case "hideInputs" -> b.hideInputs(true);
            case "hideInputMessages" -> b.hideInputMessages(true);
            case "hideOutputs" -> b.hideOutputs(true);
            default -> b.hideOutputMessages(true);
        }
        List<SpanData> spans = run(b.build(), c -> invoke(c, path));
        assertThat(spans).isNotEmpty();
        String secret = flag.startsWith("hideInput") ? IN : OUT;
        assertThat(leakedKeys(spans, secret)).as("keys exported with the sentinel").isEmpty();
    }
}
