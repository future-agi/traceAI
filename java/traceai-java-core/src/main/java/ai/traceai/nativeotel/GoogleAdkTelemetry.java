package ai.traceai.nativeotel;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.context.Context;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.net.URI;
import java.time.Duration;
import java.util.Objects;
import java.util.concurrent.TimeUnit;

/**
 * Exports Google ADK Java's native spans without installing a second span producer.
 *
 * <p>Call {@link #register(Config)} before ADK loads its telemetry class. This class deliberately
 * has no ADK dependency: ADK captures the global tracer at class initialization. Applications that
 * already own an SDK can instead add {@link #spanProcessor(SpanExporter, boolean)} and merge
 * {@link #resource(Config)} before registering that SDK. Do not install both routes.
 *
 * <p>Content filtering and attribute mapping are local to this processor's exporter. They do not
 * sanitize other exporters, application logs, span names, or application-defined metadata.
 */
public final class GoogleAdkTelemetry {
    static final String ADK_SCOPE = "gcp.vertex.agent";

    private GoogleAdkTelemetry() {}

    /** Future AGI destination. Credentials are deliberately excluded from {@code toString()}. */
    public record Config(String baseUrl, String apiKey, String secretKey, String projectName) {
        public Config {
            requireValue(baseUrl, "FI_BASE_URL");
            requireValue(apiKey, "FI_API_KEY");
            requireValue(secretKey, "FI_SECRET_KEY");
            requireValue(projectName, "FI_PROJECT_NAME");
            URI uri;
            try {
                uri = URI.create(baseUrl);
            } catch (IllegalArgumentException e) {
                throw new IllegalArgumentException("FI_BASE_URL must be an absolute HTTP(S) base URL");
            }
            if (!("https".equals(uri.getScheme()) || "http".equals(uri.getScheme()))
                    || uri.getHost() == null || uri.getUserInfo() != null
                    || uri.getQuery() != null || uri.getFragment() != null) {
                throw new IllegalArgumentException("FI_BASE_URL must be an absolute HTTP(S) base URL without credentials, query or fragment");
            }
        }

        /** Reads the four FI destination variables; none are logged. */
        public static Config fromEnv() {
            return new Config(System.getenv("FI_BASE_URL"), System.getenv("FI_API_KEY"),
                    System.getenv("FI_SECRET_KEY"), System.getenv("FI_PROJECT_NAME"));
        }

        /** Full OTLP/HTTP trace endpoint, including the Future AGI path. */
        public String tracesEndpoint() {
            return baseUrl.replaceAll("/+$", "") + "/tracer/v1/traces";
        }

        @Override
        public String toString() {
            return "GoogleAdkTelemetry.Config[credentials redacted]";
        }
    }

    private static void requireValue(String value, String name) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Set " + name);
        }
    }

    /** Content is private unless FI_ADK_CAPTURE_CONTENT is explicitly true. */
    public static boolean captureContentFromEnv() {
        return "true".equalsIgnoreCase(System.getenv("FI_ADK_CAPTURE_CONTENT"));
    }

    /** Registers one owned SDK and OTLP/HTTP exporter, before any ADK code runs. */
    public static OpenTelemetrySdk register(Config config) {
        Objects.requireNonNull(config, "config");
        return register(config, OtlpHttpSpanExporter.builder()
                .setEndpoint(config.tracesEndpoint())
                .addHeader("X-Api-Key", config.apiKey())
                .addHeader("X-Secret-Key", config.secretKey())
                .setCompression("none")
                .setTimeout(Duration.ofSeconds(10))
                .build());
    }

    /** Registers an SDK that owns the supplied exporter, using the environment privacy setting. */
    public static OpenTelemetrySdk register(Config config, SpanExporter exporter) {
        return register(config, exporter, captureContentFromEnv());
    }

    /** Registers an owned SDK with explicit content capture. A late/second registration fails. */
    public static OpenTelemetrySdk register(Config config, SpanExporter exporter, boolean captureContent) {
        OpenTelemetrySdk sdk = build(config, exporter, captureContent);
        try {
            GlobalOpenTelemetry.set(sdk);
            return sdk;
        } catch (RuntimeException e) {
            // A failed registration must not leave an exporter or batch thread running.
            shutdown(sdk);
            throw e;
        }
    }

    /** Builds an owned SDK without reading or changing the global instance. */
    public static OpenTelemetrySdk build(Config config, SpanExporter exporter, boolean captureContent) {
        SdkTracerProvider provider = SdkTracerProvider.builder()
                .setResource(Resource.getDefault().merge(resource(config)))
                .addSpanProcessor(spanProcessor(exporter, captureContent))
                .build();
        return OpenTelemetrySdk.builder().setTracerProvider(provider).build();
    }

    /** Routing attributes to merge into an application-owned SDK's resource. */
    public static Resource resource(Config config) {
        Objects.requireNonNull(config, "config");
        return Resource.create(Attributes.of(
                AttributeKey.stringKey("project_name"), config.projectName(),
                AttributeKey.stringKey("project_type"), "observe",
                AttributeKey.stringKey("service.name"), config.projectName()));
    }

    /**
     * Creates one batched destination pipeline, including baggage enrichment and export-local
     * mapping/privacy. The returned processor owns the exporter; add it once to the SDK builder.
     */
    public static SpanProcessor spanProcessor(SpanExporter exporter, boolean captureContent) {
        SpanProcessor batch = BatchSpanProcessor.builder(
                new GoogleAdkSpanExporter(Objects.requireNonNull(exporter, "exporter"), captureContent)).build();
        return SpanProcessor.composite(new BaggageProcessor(), batch);
    }

    /** Flushes pending spans and closes the owned provider/exporter before process exit. */
    public static void shutdown(OpenTelemetrySdk sdk) {
        Objects.requireNonNull(sdk, "sdk").getSdkTracerProvider().shutdown().join(15, TimeUnit.SECONDS);
    }

    private static final class BaggageProcessor implements SpanProcessor {
        @Override
        public void onStart(Context parent, ReadWriteSpan span) {
            if (!ADK_SCOPE.equals(span.getInstrumentationScopeInfo().getName())) {
                return;
            }
            Baggage baggage = Baggage.fromContext(parent);
            for (String key : new String[] {"session.id", "user.id"}) {
                String value = baggage.getEntryValue(key);
                if (value != null) {
                    span.setAttribute(key, value);
                }
            }
        }

        @Override public boolean isStartRequired() { return true; }
        @Override public void onEnd(ReadableSpan span) {}
        @Override public boolean isEndRequired() { return false; }
    }
}
