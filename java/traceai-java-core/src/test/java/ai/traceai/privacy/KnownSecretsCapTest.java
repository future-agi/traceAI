package ai.traceai.privacy;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.TraceFlags;
import io.opentelemetry.api.trace.TraceState;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.logging.Handler;
import java.util.logging.LogRecord;
import java.util.logging.Logger;

import static org.assertj.core.api.Assertions.assertThat;

/** Capture-time caps leave no prefix of a known secret; warnings, bounds, resource, scope and links. */
class KnownSecretsCapTest {
    private static final String R = KnownSecrets.PLACEHOLDER;
    private static final String M = KnownSecrets.TRUNCATION_MARKER;
    private static final String SECRET = "PFX-STRADDLE-SECRET-VALUE";
    private final Logger logger = Logger.getLogger(KnownSecrets.class.getName());
    private final List<String> warnings = new CopyOnWriteArrayList<>();
    private final Handler handler = new Handler() {
        @Override public void publish(LogRecord record) {
            warnings.add(new java.util.logging.SimpleFormatter().formatMessage(record));
        }
        @Override public void flush() { }
        @Override public void close() { }
    };

    @BeforeEach
    void attach() {
        KnownSecrets.resetWarningsForTest();
        logger.addHandler(handler);
    }

    @AfterEach
    void detach() {
        logger.removeHandler(handler);
    }

    private static KnownSecrets registry(String... values) {
        KnownSecrets registry = new KnownSecrets();
        for (String value : values) {
            registry.register(value);
        }
        return registry;
    }

    @Test
    void aFullValueIsReplacedBeforeItIsCutAtEveryOffsetOfTheSecret() {
        KnownSecrets.Snapshot snapshot = registry(SECRET).snapshot();
        for (int inside = 0; inside <= SECRET.length(); inside++) {
            String value = "a".repeat(32000 - M.length() - inside) + SECRET + "tail".repeat(50);
            String capped = snapshot.cap(value, 32000);
            assertThat(capped).as("secret cut after %d characters", inside)
                .doesNotContain("PFX").endsWith(M).hasSizeLessThanOrEqualTo(32000);
        }
    }

    @Test
    void textAlreadyCutLosesTheTailThatCouldStartASecret() {
        KnownSecrets.Snapshot snapshot = registry(SECRET).snapshot();
        for (int inside = 1; inside < SECRET.length(); inside++) {
            String cut = "a".repeat(100) + SECRET.substring(0, inside);
            assertThat(snapshot.sealCut(cut)).as("%d characters of the secret before the cut", inside)
                .isEqualTo("a".repeat(100 + inside - (SECRET.length() - 1)) + M);
        }
        assertThat(snapshot.sealCut("see " + SECRET + " here")).startsWith("see " + R).endsWith(M);
    }

    /** Pins the drop of the last longest()-1 characters: without it more of the cut text would survive. */
    @Test
    void sealCutDropsExactlyTheCharactersThatCouldStartASecret() {
        KnownSecrets.Snapshot snapshot = registry(SECRET).snapshot();
        int drop = SECRET.length() - 1;
        for (int inside : new int[] {1, 2, 3, 10, 24}) {
            String cut = "a".repeat(100) + SECRET.substring(0, inside);
            assertThat(snapshot.sealCut(cut)).as("%d characters of the secret", inside)
                .isEqualTo("a".repeat(100 + inside - drop) + M);
        }
        // Nothing to drop when no secret is known.
        assertThat(new KnownSecrets().snapshot().sealCut("abc")).isEqualTo("abc" + M);
    }

    @Test
    void everyMarkerInAValueIsACutPointAtExport() {
        String late = "PFX-LATE-LEARNED-LONGER-SECRET-0123456789";
        KnownSecrets.Snapshot snapshot = registry(late).snapshot();
        for (int inside : new int[] {1, 2, 5, 17}) {
            String json = "[{\"content\":\"" + "a".repeat(30) + late.substring(0, inside) + M + "\",\"role\":\"user\"}]";
            assertThat(snapshot.scrub(json)).as("%d characters before a marker inside JSON", inside)
                .isEqualTo("[{\"content\":\"" + "a".repeat(30) + R + M + "\",\"role\":\"user\"}]");
        }
        String two = "x" + late.substring(0, 3) + M + "y" + late.substring(0, 6) + M + "z";
        assertThat(snapshot.scrub(two)).isEqualTo("x" + R + M + "y" + R + M + "z");
        // A prefix that is not directly before a marker is ordinary text.
        assertThat(snapshot.scrub(late.substring(0, 6) + " and " + M)).startsWith(late.substring(0, 6));
    }

    @Test
    void aSecretIsAlsoReplacedInItsJsonEscapedForm() {
        String secret = "p<a>s&s'w\"o\\rd=";
        String escaped = "p<a>s&s'w\\\"o\\\\rd=";
        KnownSecrets.Snapshot snapshot = registry(secret).snapshot();
        assertThat(snapshot.scrub("raw " + secret + " end")).isEqualTo("raw " + R + " end");
        assertThat(snapshot.scrub("json " + escaped + " end")).isEqualTo("json " + R + " end");
        String controls = "line1\nline2\ttab-secret-xyz";
        assertThat(registry(controls).snapshot().scrub("v=line1\\nline2\\ttab-secret-xyz!")).isEqualTo("v=" + R + "!");
    }

    @Test
    void capAndSealCutNeverThrow() {
        KnownSecrets.Snapshot snapshot = registry(SECRET).snapshot();
        assertThat(snapshot.sealCut(null)).isNull();
        assertThat(snapshot.cap("x".repeat(100), 5)).isEmpty();
        assertThat(snapshot.cap("x".repeat(100), 0)).isEmpty();
        assertThat(snapshot.cap("x".repeat(100), -3)).isEmpty();
        assertThat(snapshot.cap(null, 10)).isNull();
        assertThat(KnownSecrets.sealCut(null)).isNull();
        assertThat(KnownSecrets.cap("x".repeat(100), 3)).isEmpty();
    }

    @Test
    void theProcessWideHelpersUseTheProcessRegistry() {
        String secret = "PFX-process-helper-secret-9876";
        KnownSecrets.process().register(secret);
        String value = "a".repeat(32000 - M.length() - 7) + secret + "tail".repeat(100);
        assertThat(KnownSecrets.cap(value, 32000)).doesNotContain("PFX").endsWith(M);
        assertThat(KnownSecrets.sealCut("b".repeat(50) + secret.substring(0, 9))).doesNotContain("PFX");
    }

    @Test
    void aValueIgnoredForItsLengthWarnsOncePerProcessWithoutTheValue() {
        KnownSecrets registry = new KnownSecrets();
        registry.register("short-1");
        registry.register("short-2");
        registry.register(null);
        registry.register("");
        assertThat(warnings).hasSize(1);
        assertThat(warnings.get(0)).contains("8").doesNotContain("short-");
        assertThat(registry.snapshot().isEmpty()).isTrue();
    }

    @Test
    void exceedingTheValueBoundDropsSpansAndWarnsExactlyOnce() {
        KnownSecrets registry = new KnownSecrets();
        for (int i = 0; i <= 1024; i++) {
            registry.register(String.format("bound-value-%06d", i));
        }
        assertThat(registry.snapshot().isUnavailable()).isTrue();
        InMemorySpanExporter delegate = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(delegate)).build();
        Span span = provider.get("t").spanBuilder("s").startSpan();
        span.end();
        List<SpanData> spans = delegate.getFinishedSpanItems();
        delegate.reset();
        SecretReplacingSpanExporter exporter = (SecretReplacingSpanExporter)
            SecretReplacingSpanExporter.wrap(delegate, registry);
        for (int i = 0; i < 3; i++) {
            assertThat(exporter.export(spans).isSuccess()).isFalse();
        }
        assertThat(delegate.getFinishedSpanItems()).isEmpty();
        assertThat(warnings).hasSize(1);
        assertThat(warnings.get(0)).contains("1024").doesNotContain("bound-value-");
        provider.close();
    }

    @Test
    void aValueOverTheCharacterBoundFailsTheRegistryClosed() {
        KnownSecrets registry = new KnownSecrets();
        registry.register("x".repeat(4 * 1024 * 1024 + 1));
        assertThat(registry.snapshot().isUnavailable()).isTrue();
        registry.snapshot().warnDropped();
        assertThat(warnings).hasSize(1);
        assertThat(warnings.get(0)).contains("characters");
    }

    @Test
    void theTotalCharacterBoundFailsTheRegistryClosed() {
        KnownSecrets registry = new KnownSecrets();
        for (int i = 0; i < 5; i++) {
            registry.register(("v" + i).repeat(1) + "y".repeat(1024 * 1024));
        }
        assertThat(registry.snapshot().isUnavailable()).isTrue();
    }

    @Test
    void textBeyondTheWorkBoundIsReplacedWhole() {
        KnownSecrets registry = new KnownSecrets();
        for (int i = 0; i < 40; i++) {
            registry.register(String.format("work-bound-secret-%04d", i));
        }
        String huge = "z".repeat(16 * 1024 * 1024 / 41 + 10);
        assertThat(registry.snapshot().scrub(huge)).isEqualTo(R);
        assertThat(registry.snapshot().scrub("small text")).isEqualTo("small text");
    }

    @Test
    void resourceScopeAndLinkAttributesAreReplacedAtExport() {
        String secret = "PFX-resource-scope-secret";
        InMemorySpanExporter raw = InMemorySpanExporter.create();
        SdkTracerProvider provider = SdkTracerProvider.builder()
            .setResource(Resource.create(Attributes.of(AttributeKey.stringKey("service.name"), "svc-" + secret,
                AttributeKey.stringArrayKey("deployment.tags"), List.of("t-" + secret, "plain"))))
            .addSpanProcessor(SimpleSpanProcessor.create(raw)).build();
        SpanContext linked = SpanContext.create("0123456789abcdef0123456789abcdef", "0123456789abcdef",
            TraceFlags.getSampled(), TraceState.getDefault());
        Span span = provider.tracerBuilder("scope-" + secret).setInstrumentationVersion("v-" + secret).build()
            .spanBuilder("s").setSpanKind(SpanKind.INTERNAL)
            .addLink(linked, Attributes.of(AttributeKey.stringKey("link.attr"), "l-" + secret,
                AttributeKey.stringArrayKey("link.arr"), List.of("a-" + secret))).startSpan();
        span.end();
        InMemorySpanExporter out = InMemorySpanExporter.create();
        SecretReplacingSpanExporter.wrap(out, registry(secret)).export(raw.getFinishedSpanItems());
        SpanData one = out.getFinishedSpanItems().get(0);
        assertThat(one.getResource().getAttribute(AttributeKey.stringKey("service.name"))).isEqualTo("svc-" + R);
        assertThat(one.getResource().getAttribute(AttributeKey.stringArrayKey("deployment.tags")))
            .containsExactly("t-" + R, "plain");
        assertThat(one.getInstrumentationScopeInfo().getName()).isEqualTo("scope-" + R);
        assertThat(one.getInstrumentationScopeInfo().getVersion()).isEqualTo("v-" + R);
        assertThat(one.getInstrumentationLibraryInfo().getName()).isEqualTo("scope-" + R);
        assertThat(one.getInstrumentationLibraryInfo().getVersion()).isEqualTo("v-" + R);
        assertThat(one.getLinks().get(0).getAttributes().get(AttributeKey.stringKey("link.attr"))).isEqualTo("l-" + R);
        assertThat(one.getLinks().get(0).getAttributes().get(AttributeKey.stringArrayKey("link.arr")))
            .containsExactly("a-" + R);
        provider.close();
    }
}
