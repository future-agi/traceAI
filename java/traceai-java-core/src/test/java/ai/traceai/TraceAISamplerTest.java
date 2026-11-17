package ai.traceai;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.TraceFlags;
import io.opentelemetry.api.trace.TraceState;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import io.opentelemetry.sdk.trace.samplers.SamplingResult;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;

/** Child JVMs give each registration a real, isolated environment without mutating System.getenv(). */
public class TraceAISamplerTest {
    @TempDir Path temporary;

    @ParameterizedTest
    @CsvSource({
        "always_on,0,true,true,true",
        "always_off,0,false,false,false",
        "traceidratio,0,false,false,false",
        "parentbased_always_on,0,true,true,false",
        "parentbased_always_off,0,false,true,false",
        "parentbased_traceidratio,0,false,true,false"
    })
    void ownedProviderUsesSixStandardSamplers(String name, String arg, boolean root, boolean sampledChild, boolean unsampledChild) throws Exception {
        String log = run("environment", Map.of("OTEL_TRACES_SAMPLER", name, "OTEL_TRACES_SAMPLER_ARG", arg));
        decisions(log, root, sampledChild, unsampledChild);
    }

    @Test void parentBasedHonorsLocalAndRemoteParentFlags() throws Exception {
        decisions(run("environment", Map.of("OTEL_TRACES_SAMPLER", "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG", "0")), false, true, false);
    }

    @Test void explicitFiRateWinsOverStandardSampler() throws Exception {
        decisions(run("environment", Map.of("FI_SAMPLE_RATE", "1", "OTEL_TRACES_SAMPLER", "always_off")), true, true, false);
        decisions(run("environment", Map.of("FI_SAMPLE_RATE", "0", "OTEL_TRACES_SAMPLER", "always_on")), false, true, false);
    }

    @ParameterizedTest
    @ValueSource(strings = {"unsupported-sampler", "ALWAYS_OFF", " always_off", "always_off ", " ALWAYS_OFF ", "TRACEIDRATIO", " traceidratio "})
    void invalidSamplerFallsBackWithOneWarning(String name) throws Exception {
        String log = run("environment", Map.of("OTEL_TRACES_SAMPLER", name));
        decisions(log, true, true, false);
        assertEquals(1, log.lines().filter(line -> line.contains("OTEL_TRACES_SAMPLER") && line.contains("default")).count(), log);
        assertFalse(log.contains(name), "warnings must not echo environment values");
    }

    @ParameterizedTest @ValueSource(strings = {"not-a-ratio", "NaN", "Infinity", "-0.1", "1.1", " "})
    void invalidRatioFallsBackWithOneWarning(String arg) throws Exception {
        String log = run("environment", Map.of("OTEL_TRACES_SAMPLER", "traceidratio", "OTEL_TRACES_SAMPLER_ARG", arg));
        decisions(log, true, true, false);
        assertEquals(1, log.lines().filter(line -> line.contains("OTEL_TRACES_SAMPLER") && line.contains("default")).count(), log);
    }

    @ParameterizedTest @ValueSource(strings = {"traceidratio", "parentbased_traceidratio"})
    void emptyRatioDefaultsToOneWithoutWarning(String name) throws Exception {
        String log = run("environment", Map.of("OTEL_TRACES_SAMPLER", name, "OTEL_TRACES_SAMPLER_ARG", ""));
        decisions(log, true, true, name.equals("traceidratio"));
        assertFalse(log.contains("OTEL_TRACES_SAMPLER"), log);
    }

    @Test void missingRatioDefaultsToOneAndUnusedArgumentIsIgnored() throws Exception {
        String defaultLog = run("environment", Map.of("OTEL_TRACES_SAMPLER", "traceidratio"));
        decisions(defaultLog, true, true, true);
        assertFalse(defaultLog.contains("OTEL_TRACES_SAMPLER"), defaultLog);
        String log = run("environment", Map.of("OTEL_TRACES_SAMPLER", "always_off", "OTEL_TRACES_SAMPLER_ARG", "not-used"));
        decisions(log, false, false, false);
        assertFalse(log.contains("OTEL_TRACES_SAMPLER"), log);
    }

    @Test void explicitRateSuppressesInvalidStandardSamplerWarning() throws Exception {
        String log = run("environment", Map.of("FI_SAMPLE_RATE", "0", "OTEL_TRACES_SAMPLER", "unsupported-sampler", "OTEL_TRACES_SAMPLER_ARG", "NaN"));
        decisions(log, false, true, false);
        assertFalse(log.contains("OTEL_TRACES_SAMPLER"), log);
    }

    @Test void optionalSamplerIsUsedAndOverridesEnvironment() throws Exception {
        decisions(run("option-off", Map.of("FI_SAMPLE_RATE", "1", "OTEL_TRACES_SAMPLER", "always_on")), false, false, false);
        decisions(run("option-on", Map.of("FI_SAMPLE_RATE", "0", "OTEL_TRACES_SAMPLER", "always_off")), true, true, true);
    }

    @Test void absentSamplerOptionKeepsParentBasedDefault() throws Exception {
        String log = run("option-absent", Map.of());
        assertTrue(log.contains("OPTION_NULL=true"), log);
        decisions(log, true, true, false);
    }

    @Test void applicationOwnedProviderKeepsItsSampler() throws Exception {
        decisions(run("borrowed-off", Map.of("FI_SAMPLE_RATE", "1", "OTEL_TRACES_SAMPLER", "always_on")), false, false, false);
        decisions(run("borrowed-on", Map.of("FI_SAMPLE_RATE", "0", "OTEL_TRACES_SAMPLER", "always_off")), true, true, true);
        String log = run("borrowed-off", Map.of("OTEL_TRACES_SAMPLER", "unsupported-sampler"));
        assertFalse(log.contains("OTEL_TRACES_SAMPLER"), log);
    }

    @Test void recordOnlySamplerDoesNotAcquireSampledFlag() throws Exception {
        String log = run("record-only", Map.of());
        assertTrue(log.contains("ROOT_RECORDING=true"), log);
        assertTrue(log.contains("ROOT_SAMPLED=false"), log);
    }

    private String run(String mode, Map<String, String> environment) throws Exception {
        Path output = Files.createTempFile(temporary, "sampler-", ".log");
        ProcessBuilder builder = new ProcessBuilder(
            Path.of(System.getProperty("java.home"), "bin", "java").toString(), "-cp",
            System.getProperty("surefire.test.class.path", System.getProperty("java.class.path")),
            TraceAISamplerTest.class.getName(), mode).redirectErrorStream(true).redirectOutput(output.toFile());
        builder.environment().keySet().removeIf(key -> key.startsWith("FI_") || key.startsWith("OTEL_"));
        builder.environment().putAll(environment);
        Process process = builder.start();
        if (!process.waitFor(20, TimeUnit.SECONDS)) {
            process.destroyForcibly();
            fail("Sampler child JVM timed out");
        }
        String log = Files.readString(output);
        assertEquals(0, process.exitValue(), log);
        return log;
    }

    private static void decisions(String log, boolean root, boolean sampledChild, boolean unsampledChild) {
        assertTrue(log.contains("ROOT_RECORDING=" + root), log);
        assertTrue(log.contains("ROOT_SAMPLED=" + root), log);
        for (String locality : new String[]{"LOCAL_", "REMOTE_"}) {
            assertTrue(log.contains(locality + "SAMPLED_RECORDING=" + sampledChild), log);
            assertTrue(log.contains(locality + "SAMPLED_SAMPLED=" + sampledChild), log);
            assertTrue(log.contains(locality + "UNSAMPLED_RECORDING=" + unsampledChild), log);
            assertTrue(log.contains(locality + "UNSAMPLED_SAMPLED=" + unsampledChild), log);
        }
    }

    public static void main(String[] args) throws Exception {
        String mode = args[0];
        // A span processor is required: newer OpenTelemetry SDKs hand out a no-op tracer when the provider has none.
        TraceConfig.Builder builder = TraceConfig.builder().projectName("sampler-test").enableConsoleExporter(true);
        Sampler option = null;
        if (mode.equals("option-off") || mode.equals("borrowed-on")) option = Sampler.alwaysOff();
        if (mode.equals("option-on") || mode.equals("borrowed-off")) option = Sampler.alwaysOn();
        if (mode.equals("record-only")) option = new Sampler() {
            @Override public SamplingResult shouldSample(Context parent, String traceId, String name,
                    io.opentelemetry.api.trace.SpanKind kind, io.opentelemetry.api.common.Attributes attributes,
                    java.util.List<io.opentelemetry.sdk.trace.data.LinkData> links) {
                return SamplingResult.recordOnly();
            }
            @Override public String getDescription() { return "record-only-test"; }
        };
        // Reflection keeps this test compilable on the base: missing additive APIs fail by test name.
        if (option != null) TraceConfig.Builder.class.getMethod("sampler", Sampler.class).invoke(builder, option);
        TraceConfig config = builder.build();
        if (mode.equals("option-absent")) System.out.println("OPTION_NULL=" + (TraceConfig.class.getMethod("getSampler").invoke(config) == null));
        SdkTracerProvider application = null;
        try {
            if (mode.startsWith("borrowed-")) {
                application = SdkTracerProvider.builder().setSampler(mode.equals("borrowed-off") ? Sampler.alwaysOff() : Sampler.alwaysOn())
                    .addSpanProcessor(io.opentelemetry.sdk.trace.export.SimpleSpanProcessor.create(
                        io.opentelemetry.exporter.logging.LoggingSpanExporter.create())).build();
                TraceAI.init(config, OpenTelemetrySdk.builder().setTracerProvider(application).build());
            } else TraceAI.init(config);
            probe("ROOT", Context.root());
            for (boolean remote : new boolean[]{false, true}) for (boolean sampled : new boolean[]{true, false}) {
                TraceFlags flags = sampled ? TraceFlags.getSampled() : TraceFlags.getDefault();
                String traceId = "12345678901234567890123456789012", spanId = "1234567890123456";
                SpanContext parent = remote
                    ? SpanContext.createFromRemoteParent(traceId, spanId, flags, TraceState.getDefault())
                    : SpanContext.create(traceId, spanId, flags, TraceState.getDefault());
                probe((remote ? "REMOTE_" : "LOCAL_") + (sampled ? "SAMPLED" : "UNSAMPLED"), Context.root().with(Span.wrap(parent)));
                if (parent.isSampled() != sampled) throw new AssertionError("Parent sampling flag changed");
            }
            TraceAI.shutdown();
            if (application != null) {
                Span after = application.get("application").spanBuilder("after-detach").setNoParent().startSpan();
                if (after.isRecording() != mode.equals("borrowed-on")) throw new AssertionError("Application provider changed");
                after.end();
            }
        } finally {
            TraceAI.shutdown();
            if (application != null) application.close();
        }
    }

    private static void probe(String name, Context parent) {
        Span span = TraceAI.getTracer().startSpan(name, FISpanKind.CHAIN, parent);
        System.out.println(name + "_RECORDING=" + span.isRecording());
        System.out.println(name + "_SAMPLED=" + span.getSpanContext().isSampled());
        if (Span.fromContext(parent).getSpanContext().isValid()
                && !span.getSpanContext().getTraceId().equals(Span.fromContext(parent).getSpanContext().getTraceId())) {
            throw new AssertionError("Parent trace identity changed");
        }
        span.end();
    }
}
