package ai.traceai.langchain4j;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import dev.langchain4j.agent.tool.ToolExecutionRequest;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.message.AiMessage;
import dev.langchain4j.data.message.ChatMessage;
import dev.langchain4j.data.message.SystemMessage;
import dev.langchain4j.data.message.UserMessage;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.chat.ChatLanguageModel;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.model.output.Response;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * Offline check that every content path the LangChain4j wrappers write (chat input and output messages,
 * tool-call arguments, embedding text, AI-service parameters and result) stays off exported spans under
 * the hide flags. The SDK models are stubbed; nothing touches the network.
 */
class ContentKeysHideFlagsTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    private static final String IN = "SENTINEL-L4J-INPUT-DO-NOT-EXPORT";
    private static final String SYS = "SENTINEL-L4J-SYSTEM-DO-NOT-EXPORT";
    private static final String OUT = "SENTINEL-L4J-OUTPUT-DO-NOT-EXPORT";
    private static final String ARGS = "{\"ssn\":\"SENTINEL-L4J-TOOLARGS-DO-NOT-EXPORT\"}";
    private static final String ARGS_SENTINEL = "SENTINEL-L4J-TOOLARGS-DO-NOT-EXPORT";

    private static FITracer tracer(TraceConfig config) {
        otel.clearSpans();
        return new FITracer(otel.getOpenTelemetry().getTracer("t"), config);
    }

    private static String everything() {
        StringBuilder sb = new StringBuilder();
        for (SpanData span : otel.getSpans()) {
            span.getAttributes().forEach((k, v) -> sb.append(k.getKey()).append('=').append(v).append('\n'));
            span.getEvents().forEach(e -> {
                sb.append(e.getName()).append('\n');
                e.getAttributes().forEach((k, v) -> sb.append(k.getKey()).append('=').append(v).append('\n'));
            });
        }
        return sb.toString();
    }

    private static List<TraceConfig> hideInputSide() {
        return List.of(TraceConfig.builder().hideInputs(true).build(),
            TraceConfig.builder().hideInputMessages(true).build());
    }

    private static List<TraceConfig> hideOutputSide() {
        return List.of(TraceConfig.builder().hideOutputs(true).build(),
            TraceConfig.builder().hideOutputMessages(true).build());
    }

    // ---- chat model ----

    private static void chat(TraceConfig config) {
        FITracer tracer = tracer(config);
        ChatLanguageModel delegate = mock(ChatLanguageModel.class);
        ToolExecutionRequest request = ToolExecutionRequest.builder()
            .id("call-1").name("lookup").arguments(ARGS).build();
        when(delegate.generate(anyList())).thenReturn(Response.from(AiMessage.from(OUT, List.of(request))));
        new TracedChatLanguageModel(delegate, tracer, "openai")
            .generate(List.<ChatMessage>of(SystemMessage.from(SYS), UserMessage.from(IN)));
    }

    @Test
    void chatContentIsExportedWhenNothingIsHidden() {
        chat(TraceConfig.builder().build());
        assertThat(everything()).contains(IN, SYS, OUT, ARGS_SENTINEL);
    }

    @Test
    void chatInputMessagesAreHiddenByInputFlags() {
        for (TraceConfig config : hideInputSide()) {
            chat(config);
            assertThat(everything()).doesNotContain(IN).doesNotContain(SYS);
        }
    }

    @Test
    void chatOutputMessagesAreHiddenByOutputFlags() {
        for (TraceConfig config : hideOutputSide()) {
            chat(config);
            assertThat(everything()).doesNotContain(OUT);
        }
    }

    @Test
    void chatToolCallArgumentsAreHiddenByEveryFlag() {
        List<TraceConfig> all = new java.util.ArrayList<>(hideInputSide());
        all.addAll(hideOutputSide());
        for (TraceConfig config : all) {
            chat(config);
            assertThat(everything()).doesNotContain(ARGS_SENTINEL);
        }
    }

    // ---- embedding model ----

    private static void embed(TraceConfig config) {
        FITracer tracer = tracer(config);
        EmbeddingModel delegate = mock(EmbeddingModel.class);
        when(delegate.embedAll(anyList())).thenReturn(Response.from(List.of(Embedding.from(new float[] {0.1f, 0.2f}))));
        new TracedEmbeddingModel(delegate, tracer, "openai").embedAll(List.of(TextSegment.from(IN)));
    }

    @Test
    void embeddingTextIsHiddenByInputFlags() {
        embed(TraceConfig.builder().build());
        assertThat(everything()).contains(IN);
        for (TraceConfig config : hideInputSide()) {
            embed(config);
            assertThat(everything()).doesNotContain(IN);
        }
    }

    // ---- AI services (method parameters and result) ----

    public interface Assistant {
        String chat(String question);
    }

    private static void aiService(TraceConfig config) {
        FITracer tracer = tracer(config);
        Assistant real = question -> OUT;
        TracedAiServices.create(Assistant.class, real, tracer).chat(IN);
    }

    @Test
    void aiServiceContentIsExportedWhenNothingIsHidden() {
        aiService(TraceConfig.builder().build());
        assertThat(everything()).contains(IN, OUT);
    }

    @Test
    void aiServiceInputIsHiddenByInputFlags() {
        for (TraceConfig config : hideInputSide()) {
            aiService(config);
            assertThat(everything()).doesNotContain(IN);
        }
    }

    @Test
    void aiServiceOutputIsHiddenByOutputFlags() {
        for (TraceConfig config : hideOutputSide()) {
            aiService(config);
            assertThat(everything()).doesNotContain(OUT);
        }
    }
}
