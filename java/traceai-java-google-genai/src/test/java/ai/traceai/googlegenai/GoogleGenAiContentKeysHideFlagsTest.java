package ai.traceai.googlegenai;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.google.genai.Chat;
import com.google.genai.Chats;
import com.google.genai.Client;
import com.google.genai.Models;
import com.google.genai.types.Content;
import com.google.genai.types.CountTokensResponse;
import com.google.genai.types.GenerateContentResponse;
import com.google.genai.types.Part;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * Offline: the real TracedGenerativeModel over a stubbed Google GenAI client (no network), one hide flag at a
 * time. Every sentinel the active flag covers must be absent from every exported attribute and event.
 */
class GoogleGenAiContentKeysHideFlagsTest {
    static final String IN_PROMPT = "SENTINEL-GENAI-IN-PROMPT";
    static final String IN_CONTENT = "SENTINEL-GENAI-IN-CONTENT";
    static final String IN_COUNT = "SENTINEL-GENAI-IN-COUNT";
    static final String IN_CHAT = "SENTINEL-GENAI-IN-CHAT";
    static final String OUT_TEXT = "SENTINEL-GENAI-OUT-TEXT";
    static final String OUT_CHAT = "SENTINEL-GENAI-OUT-CHAT";

    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private SdkTracerProvider provider;
    private Client client;

    private static GenerateContentResponse response(String text) {
        return GenerateContentResponse.fromJson("{\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":"
            + "[{\"text\":\"" + text + "\"}]},\"finishReason\":\"STOP\"}],"
            + "\"usageMetadata\":{\"promptTokenCount\":3,\"candidatesTokenCount\":5,\"totalTokenCount\":8}}");
    }

    private static void setField(Object target, String name, Object value) throws Exception {
        Field f = target.getClass().getSuperclass() == Object.class || hasField(target.getClass(), name)
            ? target.getClass().getDeclaredField(name) : target.getClass().getSuperclass().getDeclaredField(name);
        f.setAccessible(true);
        f.set(target, value);
    }

    private static boolean hasField(Class<?> type, String name) {
        try {
            type.getDeclaredField(name);
            return true;
        } catch (NoSuchFieldException e) {
            return false;
        }
    }

    @BeforeEach
    void setup() throws Exception {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        client = mock(Client.class);
        Models models = mock(Models.class);
        when(models.generateContent(anyString(), anyString(), any())).thenReturn(response(OUT_TEXT));
        when(models.generateContent(anyString(), any(List.class), any())).thenReturn(response(OUT_TEXT));
        when(models.countTokens(anyString(), anyString(), any()))
            .thenReturn(CountTokensResponse.fromJson("{\"totalTokens\":7}"));
        Chats chats = mock(Chats.class);
        Chat chat = mock(Chat.class);
        when(chat.sendMessage(anyString())).thenReturn(response(OUT_CHAT));
        when(chats.create(anyString())).thenReturn(chat);
        setField(client, "models", models);
        setField(client, "chats", chats);
    }

    @AfterEach
    void teardown() {
        provider.close();
    }

    private void run(String flag) {
        TraceConfig.Builder b = TraceConfig.builder().apiKey("offline").secretKey("offline").projectName("offline")
            .hideInputs(false).hideOutputs(false).hideInputMessages(false).hideOutputMessages(false)
            .hideErrorDetails(false);
        switch (flag) {
            case "hideInputs" -> b.hideInputs(true);
            case "hideOutputs" -> b.hideOutputs(true);
            case "hideInputMessages" -> b.hideInputMessages(true);
            case "hideOutputMessages" -> b.hideOutputMessages(true);
            default -> throw new IllegalArgumentException(flag);
        }
        TracedGenerativeModel model = new TracedGenerativeModel(client,
            new FITracer(provider.get("genai-hide-test"), b.build()), "gemini-2.0-flash");
        model.generateContent(IN_PROMPT);
        model.generateContent(List.of(Content.builder().role("user").parts(List.of(Part.fromText(IN_CONTENT))).build()));
        model.countTokens(IN_COUNT);
        model.startChat().sendMessage(IN_CHAT);
        assertThat(exporter.getFinishedSpanItems()).hasSize(4);
    }

    /** Attribute keys (and event names) whose exported value contains the sentinel. */
    private List<String> leakedKeys(String sentinel) {
        List<String> keys = new ArrayList<>();
        for (SpanData span : exporter.getFinishedSpanItems()) {
            span.getAttributes().forEach((k, v) -> { if (String.valueOf(v).contains(sentinel)) keys.add(k.getKey()); });
            span.getEvents().forEach(e -> e.getAttributes().forEach((k, v) -> {
                if (String.valueOf(v).contains(sentinel)) keys.add("event:" + e.getName() + "/" + k.getKey());
            }));
        }
        return keys;
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideInputs", "hideInputMessages"})
    void inputFlagsKeepPromptContentOutOfEverySpan(String flag) {
        run(flag);
        for (String s : List.of(IN_PROMPT, IN_CONTENT, IN_COUNT, IN_CHAT)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        // the other direction is not hidden: guards against asserting on an empty export
        assertThat(leakedKeys(OUT_TEXT)).isNotEmpty();
        assertThat(leakedKeys(OUT_CHAT)).isNotEmpty();
    }

    @ParameterizedTest
    @ValueSource(strings = {"hideOutputs", "hideOutputMessages"})
    void outputFlagsKeepResponseContentOutOfEverySpan(String flag) {
        run(flag);
        for (String s : List.of(OUT_TEXT, OUT_CHAT)) {
            assertThat(leakedKeys(s)).as("%s leaked under %s", s, flag).isEmpty();
        }
        assertThat(leakedKeys(IN_PROMPT)).isNotEmpty();
    }
}
