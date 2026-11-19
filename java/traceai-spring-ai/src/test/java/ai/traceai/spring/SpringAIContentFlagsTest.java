package ai.traceai.spring;

import ai.traceai.TraceConfig;
import io.opentelemetry.sdk.trace.data.EventData;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.embedding.Embedding;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.embedding.EmbeddingRequest;
import org.springframework.ai.embedding.EmbeddingResponse;
import org.springframework.ai.document.Document;
import reactor.core.publisher.Flux;
import reactor.test.StepVerifier;

import java.time.Duration;
import java.util.List;
import java.util.function.Function;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The content the traced Spring AI models write (prompt text, completion text, embedding input text), under each
 * hide flag, checked against every attribute and event of every exported span. No network: the delegate models
 * are in-process stubs.
 */
class SpringAIContentFlagsTest {
    private static final Duration TIMEOUT = Duration.ofSeconds(5);
    static final String IN = "SENTINEL-PROMPT-5b1d";
    static final String OUT = "SENTINEL-COMPLETION-5b1d";
    static final String EMBED = "SENTINEL-EMBEDDING-TEXT-5b1d";

    private static String everything(SpanData data) {
        StringBuilder all = new StringBuilder(data.getAttributes().toString());
        for (EventData event : data.getEvents()) all.append(event.getName()).append(event.getAttributes());
        return all.toString();
    }

    private static Prompt prompt() {
        return new Prompt(List.of(new SystemMessage(IN + "-system"), new UserMessage(IN + "-user"),
            new AssistantMessage(IN + "-assistant")));
    }

    private static String chatCall(TraceConfig config) {
        try (SpanTestSupport support = new SpanTestSupport(config)) {
            new TracedChatModel(SpanTestSupport.syncModel(p -> SpanTestSupport.response(OUT + "-call")), support.tracer, "synthetic")
                .call(prompt());
            return everything(support.single());
        }
    }

    private static String chatStream(TraceConfig config) {
        try (SpanTestSupport support = new SpanTestSupport(config)) {
            StepVerifier.create(new TracedChatModel(SpanTestSupport.streamModel(
                    () -> Flux.<ChatResponse>just(SpanTestSupport.response(OUT + "-stream"))), support.tracer, "synthetic")
                    .stream(prompt()))
                .expectNextCount(1).expectComplete().verify(TIMEOUT);
            return everything(support.single());
        }
    }

    private static final EmbeddingModel STUB_EMBEDDINGS = new EmbeddingModel() {
        @Override public EmbeddingResponse call(EmbeddingRequest request) {
            return new EmbeddingResponse(List.of(new Embedding(new float[]{1f, 2f}, 0)));
        }
        @Override public float[] embed(Document document) { return new float[]{1f}; }
    };

    private static String embeddingCall(TraceConfig config) {
        try (SpanTestSupport support = new SpanTestSupport(config)) {
            new TracedEmbeddingModel(STUB_EMBEDDINGS, support.tracer, "synthetic")
                .call(new EmbeddingRequest(List.of(EMBED + "-a", EMBED + "-b"), null));
            return everything(support.single());
        }
    }

    private static void forEachPath(TraceConfig config, Function<String, Void> check) {
        check.apply(chatCall(config));
        check.apply(chatStream(config));
        check.apply(embeddingCall(config));
    }

    @Test
    void visibleByDefaultSoTheSentinelsAreReal() {
        TraceConfig none = TraceConfig.builder().build();
        assertThat(chatCall(none)).contains(IN + "-user", OUT + "-call");
        assertThat(chatStream(none)).contains(IN + "-user", OUT + "-stream");
        assertThat(embeddingCall(none)).contains(EMBED + "-a");
    }

    @Test
    void hideInputsHidesPromptAndEmbeddingText() {
        forEachPath(TraceConfig.builder().hideInputs(true).build(), all -> {
            assertThat(all).doesNotContain(IN).doesNotContain(EMBED);
            return null;
        });
    }

    @Test
    void hideInputMessagesHidesPromptAndEmbeddingText() {
        forEachPath(TraceConfig.builder().hideInputMessages(true).build(), all -> {
            assertThat(all).doesNotContain(IN).doesNotContain(EMBED);
            return null;
        });
    }

    @Test
    void hideOutputsHidesCompletion() {
        forEachPath(TraceConfig.builder().hideOutputs(true).build(), all -> {
            assertThat(all).doesNotContain(OUT);
            return null;
        });
    }

    @Test
    void hideOutputMessagesHidesCompletion() {
        forEachPath(TraceConfig.builder().hideOutputMessages(true).build(), all -> {
            assertThat(all).doesNotContain(OUT);
            return null;
        });
    }

    @Test
    void allFlagsLeaveNoContent() {
        TraceConfig config = TraceConfig.builder().hideInputs(true).hideOutputs(true)
            .hideInputMessages(true).hideOutputMessages(true).build();
        forEachPath(config, all -> {
            assertThat(all).doesNotContain("SENTINEL");
            return null;
        });
    }
}
