package ai.traceai;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Long, double, boolean and array values on a content key are masked by key, with the same flags and
 * the same placeholder rules as strings: a redacted key keeps the string placeholder, an omitted key is
 * dropped. The string path is the oracle, so the two can never disagree.
 */
class TypedContentMaskTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    static final String REDACTED = "__REDACTED__";
    static final List<String> INPUT = ContentMaskParityTest.INPUT_KEYS;
    static final List<String> OUTPUT = ContentMaskParityTest.OUTPUT_KEYS;
    static final List<String> EITHER = ContentMaskParityTest.EITHER_KEYS;
    static final List<String> MESSAGES = List.of(
        "gen_ai.input.messages.0.message.content", "gen_ai.output.messages.0.message.content",
        "embedding.embeddings.0.embedding.text", "embedding.input_text.0",
        "message.tool_calls.0.tool_call.function.arguments");
    static final List<String> COUNTS = List.of(
        "gen_ai.usage.input_tokens", "gen_ai.retrieval.top_k", "gen_ai.tool.call.id", "tool_call.function.arguments_count");

    static final Object[] SENTINELS = {4242L, 0.4244, true, new long[] {4242L}, new double[] {0.4244}, new boolean[] {true}};

    static TraceConfig config(boolean in, boolean out, boolean inMsg, boolean outMsg) {
        return TraceConfig.builder().hideInputs(in).hideOutputs(out).hideInputMessages(inMsg).hideOutputMessages(outMsg).build();
    }

    static List<TraceConfig> configs() {
        return List.of(config(false, false, false, false), config(true, false, false, false),
            config(false, true, false, false), config(false, false, true, false), config(false, false, false, true),
            config(true, true, true, true));
    }

    /** FITracer.setAttribute for the value's type, or (before the typed setters exist) a direct span write. */
    private static void write(FITracer tracer, Span span, String key, Object value) throws Exception {
        Class<?> type = value instanceof Long ? long.class : value instanceof Double ? double.class
            : value instanceof Boolean ? boolean.class : value.getClass();
        try {
            Method method = FITracer.class.getMethod("setAttribute", Span.class, String.class, type);
            method.invoke(tracer, span, key, value);
        } catch (NoSuchMethodException missing) {
            if (value instanceof Long l) span.setAttribute(key, l);
            else if (value instanceof Double d) span.setAttribute(key, d);
            else if (value instanceof Boolean b) span.setAttribute(key, b);
            else if (value instanceof long[] a) span.setAttribute(AttributeKey.longArrayKey(key), toList(a));
            else if (value instanceof double[] a) span.setAttribute(AttributeKey.doubleArrayKey(key), toList(a));
            else if (value instanceof boolean[] a) span.setAttribute(AttributeKey.booleanArrayKey(key), toList(a));
        }
    }

    private static List<Long> toList(long[] a) { List<Long> l = new ArrayList<>(); for (long v : a) l.add(v); return l; }
    private static List<Double> toList(double[] a) { List<Double> l = new ArrayList<>(); for (double v : a) l.add(v); return l; }
    private static List<Boolean> toList(boolean[] a) { List<Boolean> l = new ArrayList<>(); for (boolean v : a) l.add(v); return l; }

    private static Attributes export(TraceConfig config, String key, Object value) throws Exception {
        otel.clearSpans();
        FITracer tracer = new FITracer(otel.getOpenTelemetry().getTracer("typed"), config);
        Span span = tracer.startSpan("typed", FISpanKind.LLM);
        if (value instanceof String s) tracer.setAttribute(span, key, s); else write(tracer, span, key, value);
        span.end();
        SpanData data = otel.getSpans().get(0);
        return data.getAttributes();
    }

    /** What the string path does with this key: null when dropped, REDACTED, or the value. */
    private static String stringDecision(TraceConfig config, String key) throws Exception {
        return export(config, key, "SENTINEL").get(AttributeKey.stringKey(key));
    }

    private static String describe(Attributes attributes, String key) {
        StringBuilder out = new StringBuilder();
        attributes.forEach((k, v) -> { if (k.getKey().equals(key)) out.append(v); });
        return out.toString();
    }

    static Stream<String> contentKeys() {
        List<String> all = new ArrayList<>(INPUT);
        all.addAll(OUTPUT);
        all.addAll(EITHER);
        all.addAll(MESSAGES);
        return all.stream();
    }

    @ParameterizedTest(name = "typed values on {0} follow the string decision")
    @MethodSource("contentKeys")
    void typedValuesOnContentKeysFollowTheStringDecision(String key) throws Exception {
        for (TraceConfig config : configs()) {
            String decision = stringDecision(config, key);
            for (Object sentinel : SENTINELS) {
                Attributes exported = export(config, key, sentinel);
                String label = key + " " + sentinel.getClass().getSimpleName() + " hideIn=" + config.isHideInputs()
                    + " hideOut=" + config.isHideOutputs() + " inMsg=" + config.isHideInputMessages()
                    + " outMsg=" + config.isHideOutputMessages();
                if (decision == null) {
                    assertThat(describe(exported, key)).as(label + " is dropped").isEmpty();
                } else if (REDACTED.equals(decision)) {
                    assertThat(exported.get(AttributeKey.stringKey(key))).as(label + " is the placeholder").isEqualTo(REDACTED);
                    assertThat(describe(exported, key)).as(label).doesNotContain("4242").doesNotContain("0.4244").doesNotContain("true");
                } else {
                    assertThat(describe(exported, key)).as(label + " is kept").isNotEmpty();
                }
            }
        }
    }

    @Test
    void namedExamplesUnderEachFlag() throws Exception {
        assertThat(export(config(true, false, false, false), "gen_ai.tool.call.arguments", 4242L)
            .get(AttributeKey.stringKey("gen_ai.tool.call.arguments"))).isEqualTo(REDACTED);
        assertThat(export(config(false, true, false, false), "gen_ai.tool.call.result", 0.4244)
            .get(AttributeKey.stringKey("gen_ai.tool.call.result"))).isEqualTo(REDACTED);
        assertThat(export(config(false, true, false, false), "gen_ai.voice.transcript", true)
            .get(AttributeKey.stringKey("gen_ai.voice.transcript"))).isEqualTo(REDACTED);
        assertThat(describe(export(config(false, false, true, false), "gen_ai.input.messages.0.message.content", 4242L),
            "gen_ai.input.messages.0.message.content")).isEmpty();
        assertThat(export(config(false, false, false, true), "output.value", 4242L)
            .get(AttributeKey.stringKey("output.value"))).isEqualTo(REDACTED);
    }

    @ParameterizedTest(name = "{0} stays visible under every flag")
    @MethodSource("counts")
    void countsAndIdentifiersAreUnchanged(String key) throws Exception {
        TraceConfig all = config(true, true, true, true);
        assertThat(export(all, key, 4242L).get(AttributeKey.longKey(key))).isEqualTo(4242L);
        assertThat(export(all, key, 0.4244).get(AttributeKey.doubleKey(key))).isEqualTo(0.4244);
        assertThat(export(all, key, true).get(AttributeKey.booleanKey(key))).isEqualTo(true);
    }

    static Stream<String> counts() { return COUNTS.stream(); }

    @ParameterizedTest(name = "no flag leaves {0} as the typed value")
    @MethodSource("contentKeys")
    void withoutAFlagTheTypedValueIsUnchanged(String key) throws Exception {
        TraceConfig none = config(false, false, false, false);
        assertThat(export(none, key, 4242L).get(AttributeKey.longKey(key))).isEqualTo(4242L);
        assertThat(export(none, key, new double[] {0.4244}).get(AttributeKey.doubleArrayKey(key))).containsExactly(0.4244);
        assertThat(export(none, key, true).get(AttributeKey.booleanKey(key))).isEqualTo(true);
    }
}
