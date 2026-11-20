package ai.traceai.privacy;

import ai.traceai.FISpanKind;
import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

class SecretReplacingSpanExporterTest {
    private static final String R = KnownSecrets.PLACEHOLDER;

    /** A span captured raw, exported afterwards through two replacing exporters (application copy, FI copy). */
    private static final class Capture implements AutoCloseable {
        final InMemorySpanExporter raw = InMemorySpanExporter.create();
        final SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(raw)).build();
        final FITracer tracer = new FITracer(provider.get("test"), TraceConfig.builder().build());

        List<SpanData> exportThrough(KnownSecrets registry) {
            List<SpanData> raws = raw.getFinishedSpanItems();
            InMemorySpanExporter app = InMemorySpanExporter.create();
            InMemorySpanExporter fi = InMemorySpanExporter.create();
            SecretReplacingSpanExporter.wrap(app, registry).export(raws);
            SecretReplacingSpanExporter.wrap(fi, registry).export(raws);
            assertThat(dump(fi.getFinishedSpanItems())).isEqualTo(dump(app.getFinishedSpanItems()));
            return fi.getFinishedSpanItems();
        }

        @Override
        public void close() {
            provider.close();
        }
    }

    /** Everything an exporter could serialize, as one string. */
    static String dump(List<SpanData> spans) {
        StringBuilder out = new StringBuilder();
        for (SpanData span : spans) {
            out.append(span.getName()).append('\n');
            span.getAttributes().forEach((k, v) -> out.append(k.getKey()).append('=').append(v).append('\n'));
            out.append(span.getStatus().getDescription()).append('\n');
            span.getEvents().forEach(e -> {
                out.append(e.getName()).append('\n');
                e.getAttributes().forEach((k, v) -> out.append(k.getKey()).append('=').append(v).append('\n'));
            });
            span.getLinks().forEach(l ->
                l.getAttributes().forEach((k, v) -> out.append(k.getKey()).append('=').append(v).append('\n')));
        }
        return out.toString();
    }

    private static KnownSecrets registry(String... secrets) {
        KnownSecrets registry = new KnownSecrets();
        for (String secret : secrets) {
            registry.register(secret);
        }
        return registry;
    }

    private static List<String[]> reversedToo(String... secrets) {
        List<String[]> orders = new ArrayList<>();
        orders.add(secrets);
        String[] reversed = new String[secrets.length];
        for (int i = 0; i < secrets.length; i++) {
            reversed[i] = secrets[secrets.length - 1 - i];
        }
        orders.add(reversed);
        return orders;
    }

    /** Captures the value raw on every surface, learns the secrets afterwards, in both orders. */
    private static void assertScrubbedEverywhere(String input, String expected, String... secrets) {
        for (String[] order : reversedToo(secrets)) {
            try (Capture capture = new Capture()) {
                Span span = capture.tracer.startSpan("name " + input, FISpanKind.LLM);
                capture.tracer.setAttribute(span, "custom.attribute", input);
                capture.tracer.setAttribute(span, "custom.array", List.of(input, "plain"));
                span.addEvent("event " + input, Attributes.of(AttributeKey.stringKey("event.attribute"), input));
                capture.tracer.setError(span, new IllegalStateException(input));
                span.end();
                String rawDump = dump(capture.raw.getFinishedSpanItems());
                for (String secret : secrets) {
                    assertThat(rawDump).as("span keeps raw values").contains(secret);
                }
                List<SpanData> exported = capture.exportThrough(registry(order));
                String out = dump(exported);
                assertThat(out).doesNotContain("PFX").doesNotContain(secrets[0]);
                SpanData one = exported.get(0);
                assertThat(one.getName()).isEqualTo("name " + expected);
                assertThat(one.getAttributes().get(AttributeKey.stringKey("custom.attribute"))).isEqualTo(expected);
                assertThat(one.getAttributes().get(AttributeKey.stringArrayKey("custom.array")))
                    .containsExactly(expected, "plain");
                assertThat(one.getEvents().get(0).getName()).isEqualTo("event " + expected);
                assertThat(one.getEvents().get(0).getAttributes().get(AttributeKey.stringKey("event.attribute")))
                    .isEqualTo(expected);
                assertThat(one.getStatus().getDescription()).isEqualTo(expected);
                assertThat(one.getAttributes().get(AttributeKey.stringKey("error.message"))).isEqualTo(expected);
                String exception = one.getEvents().stream().filter(e -> e.getName().equals("exception"))
                    .map(e -> e.getAttributes().get(AttributeKey.stringKey("exception.message")))
                    .collect(Collectors.joining());
                assertThat(exception).isEqualTo(expected);
                String stack = one.getEvents().stream().filter(e -> e.getName().equals("exception"))
                    .map(e -> e.getAttributes().get(AttributeKey.stringKey("exception.stacktrace")))
                    .collect(Collectors.joining());
                assertThat(stack).doesNotContain(secrets[0]);
                for (String secret : secrets) {
                    assertThat(stack).doesNotContain(secret);
                }
            }
        }
    }

    @Test
    void shorterLearnedFirstThenLongerContainingValue() {
        assertScrubbedEverywhere("see PFX-SHORT1-LONGPART-ZZ end", "see " + R + " end",
            "PFX-SHORT1", "PFX-SHORT1-LONGPART-ZZ");
    }

    @Test
    void oneSecretIsSubstringOfAnother() {
        assertScrubbedEverywhere("x PFX-inner-secret-in-outer y", "x " + R + " y",
            "PFX-inner-secret-in-outer", "inner-secret");
    }

    @Test
    void edgesOverlapEqualLength() {
        assertScrubbedEverywhere("PFX0-AAAA-BBBB-CCCC", R, "PFX0-AAAA-BBBB", "AAAA-BBBB-CCCC");
        assertScrubbedEverywhere("PFX0AAAABBBBCCCC", R, "PFX0AAAABBBB", "AAAABBBBCCCC");
    }

    @Test
    void oneTwoAndThreeCharacterEdgeOverlaps() {
        assertScrubbedEverywhere("PFXAAAAXBBBBBBB", R, "PFXAAAAX", "XBBBBBBB");
        assertScrubbedEverywhere("PFXAAAXYBBBBBB", R, "PFXAAAXY", "XYBBBBBB");
        assertScrubbedEverywhere("PFXAAXYZBBBBB", R, "PFXAAXYZ", "XYZBBBBB");
        assertScrubbedEverywhere("<PFXAAXYZBBBBB>", "<" + R + ">", "PFXAAXYZ", "XYZBBBBB");
    }

    @Test
    void threeSecretEdgeChain() {
        assertScrubbedEverywhere("..PFX1AAAA2BBBB3CCCC4DDD..", ".." + R + "..",
            "PFX1AAAA2BBBB", "2BBBB3CCCC", "3CCCC4DDD");
    }

    @Test
    void fourSecretEdgeChain() {
        assertScrubbedEverywhere("PFX1aaaa2bbbb3cccc4dddd5eeee", R,
            "PFX1aaaa2bbbb", "2bbbb3cccc", "3cccc4dddd", "4dddd5eeee");
    }

    @Test
    void twoInnerSecretsInsideOneOuter() {
        assertScrubbedEverywhere("o PFX-outer-aaaaaaaa-bbbbbbbb-end o", "o " + R + " o",
            "PFX-outer-aaaaaaaa-bbbbbbbb-end", "aaaaaaaa", "bbbbbbbb");
    }

    @Test
    void separatedSecretsKeepTheTextBetween() {
        assertScrubbedEverywhere("PFXsecret1 and PFXsecret2", R + " and " + R, "PFXsecret1", "PFXsecret2");
    }

    @Test
    void secretStraddlingTheLengthCapIsNotLeftHalfVisible() {
        String secret = "PFX-STRADDLE-SECRET-VALUE";
        int cut = 32000 - 13;
        String filler = "a".repeat(cut - 6);
        // The cap keeps the first 6 characters of the secret, then appends the marker.
        String input = filler + secret + "tail".repeat(100);
        for (String[] order : reversedToo(secret, "tail-unrelated-later")) {
            try (Capture capture = new Capture()) {
                Span span = capture.tracer.startSpan("cap", FISpanKind.LLM);
                capture.tracer.setAttribute(span, "custom.attribute", input);
                span.end();
                String kept = capture.raw.getFinishedSpanItems().get(0).getAttributes()
                    .get(AttributeKey.stringKey("custom.attribute"));
                assertThat(kept).endsWith(filler + "PFX-ST" + KnownSecrets.TRUNCATION_MARKER);
                String out = capture.exportThrough(registry(order)).get(0).getAttributes()
                    .get(AttributeKey.stringKey("custom.attribute"));
                assertThat(out).isEqualTo(filler + R + KnownSecrets.TRUNCATION_MARKER);
            }
        }
    }

    @Test
    void anUnrelatedValueIsReturnedUnchanged() {
        KnownSecrets registry = registry("PFX-some-secret");
        String text = "nothing to see here " + KnownSecrets.TRUNCATION_MARKER;
        assertThat(registry.snapshot().scrub(text)).isSameAs(text);
    }

    @Test
    void secretsLearnedAfterCaptureAreStillReplaced() {
        KnownSecrets registry = new KnownSecrets();
        try (Capture capture = new Capture()) {
            Span span = capture.tracer.startSpan("late", FISpanKind.LLM);
            capture.tracer.setAttribute(span, "custom.attribute", "token PFX-learned-later here");
            span.end();
            assertThat(dump(capture.exportThrough(registry))).contains("PFX-learned-later");
            registry.register("PFX-learned-later");
            assertThat(dump(capture.exportThrough(registry))).doesNotContain("PFX-learned-later").contains(R);
        }
    }

    @Test
    void registryThatFailedClosedDropsEverything() {
        KnownSecrets registry = registry("PFX-some-secret");
        registry.failClosed();
        InMemorySpanExporter out = InMemorySpanExporter.create();
        SpanExporter wrapped = SecretReplacingSpanExporter.wrap(out, registry);
        try (Capture capture = new Capture()) {
            Span span = capture.tracer.startSpan("dropped", FISpanKind.LLM);
            span.end();
            assertThat(wrapped.export(capture.raw.getFinishedSpanItems()).isSuccess()).isFalse();
            assertThat(out.getFinishedSpanItems()).isEmpty();
        }
    }

    @Test
    void shortValuesAreNotRegisteredAndOverflowFailsClosed() {
        KnownSecrets registry = new KnownSecrets();
        registry.register("short");
        registry.register(null);
        assertThat(registry.snapshot().isEmpty()).isTrue();
        for (int i = 0; i < 1025; i++) {
            registry.register("overflow-value-" + i);
        }
        assertThat(registry.snapshot().isUnavailable()).isTrue();
    }

    @Test
    void wrappingTwiceReturnsTheSameExporter() {
        KnownSecrets registry = new KnownSecrets();
        SpanExporter once = SecretReplacingSpanExporter.wrap(InMemorySpanExporter.create(), registry);
        assertThat(SecretReplacingSpanExporter.wrap(once, registry)).isSameAs(once);
    }
}
