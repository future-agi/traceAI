package ai.traceai;

import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SdkTracerProviderBuilder;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.exporter.logging.LoggingSpanExporter;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.time.Duration;
import java.util.Properties;
import java.util.concurrent.TimeUnit;

/**
 * Main entry point for TraceAI initialization.
 *
 * <p>Usage:</p>
 * <pre>
 * TraceAI.init(TraceConfig.builder()
 *     .baseUrl("https://api.futureagi.com")
 *     .apiKey("your-api-key")
 *     .projectName("my-project")
 *     .build());
 *
 * // Get the tracer for instrumentation
 * FITracer tracer = TraceAI.getTracer();
 * </pre>
 */
public final class TraceAI {

    private static final Logger logger = LoggerFactory.getLogger(TraceAI.class);
    private static final String TRACER_NAME = "traceai-java";

    private static FITracer instance;
    // Non-null only for the provider constructed by init(config), never for a borrowed SDK.
    private static SdkTracerProvider ownedTracerProvider;
    private static boolean shutdownHookRegistered;
    private static boolean initialized = false;
    private static final Object lock = new Object();

    /**
     * Initializes TraceAI with its own tracer provider and configured exporters.
     * This method is idempotent - calling it multiple times has no effect until shutdown.
     *
     * <p>The provider is local to TraceAI. It neither registers nor replaces global
     * OpenTelemetry, so initialization also works with a Java agent and after shutdown.
     * Spans created through global tracers do not use this provider. To share an existing
     * SDK's export pipeline instead, use {@link #init(TraceConfig, OpenTelemetry)}.</p>
     *
     * @param config the trace configuration
     * @throws IllegalArgumentException if config is null
     */
    public static void init(TraceConfig config) {
        if (config == null) {
            throw new IllegalArgumentException("TraceConfig cannot be null");
        }

        synchronized (lock) {
            if (initialized) {
                logger.debug("TraceAI already initialized, skipping");
                return;
            }

            // One hook for all owned-provider lifetimes, not one per reinitialization.
            // Register before allocating resources, since registration can fail during JVM shutdown.
            if (!shutdownHookRegistered) {
                Runtime.getRuntime().addShutdownHook(new Thread(TraceAI::shutdown, "traceai-shutdown"));
                shutdownHookRegistered = true;
            }

            // Build resource with service name
            String serviceName = config.getServiceName() != null
                ? config.getServiceName()
                : config.getProjectName() != null
                    ? config.getProjectName()
                    : "traceai-java-app";

            var resourceAttrsBuilder = Attributes.builder()
                .put(AttributeKey.stringKey("service.name"), serviceName);

            if (config.getProjectName() != null) {
                resourceAttrsBuilder.put(AttributeKey.stringKey("project_name"), config.getProjectName());
            }
            resourceAttrsBuilder.put(AttributeKey.stringKey("project_type"), "observe");

            Resource resource = Resource.getDefault()
                .merge(Resource.create(resourceAttrsBuilder.build()));

            // Build tracer provider
            SdkTracerProviderBuilder providerBuilder = SdkTracerProvider.builder()
                .setResource(resource);

            // Add OTLP exporter if configured
            if (config.getBaseUrl() != null && config.getApiKey() != null) {
                String baseUrl = config.getBaseUrl().endsWith("/")
                    ? config.getBaseUrl().substring(0, config.getBaseUrl().length() - 1)
                    : config.getBaseUrl();
                String endpoint = baseUrl + "/tracer/v1/traces";

                var exporterBuilder = OtlpHttpSpanExporter.builder()
                    .setEndpoint(endpoint)
                    .addHeader("X-Api-Key", config.getApiKey())
                    .addHeader("fi-project-name", config.getProjectName() != null ? config.getProjectName() : "")
                    .setTimeout(Duration.ofSeconds(30));

                if (config.getSecretKey() != null) {
                    exporterBuilder.addHeader("X-Secret-Key", config.getSecretKey());
                }

                OtlpHttpSpanExporter otlpExporter = exporterBuilder.build();

                BatchSpanProcessor batchProcessor = BatchSpanProcessor.builder(otlpExporter)
                    .setMaxExportBatchSize(config.getBatchSize())
                    .setScheduleDelay(config.getExportIntervalMs(), TimeUnit.MILLISECONDS)
                    .build();

                providerBuilder.addSpanProcessor(batchProcessor);
                logger.info("TraceAI OTLP HTTP exporter configured for endpoint: {}", endpoint);
            }

            // Add console exporter if enabled
            if (config.isEnableConsoleExporter()) {
                providerBuilder.addSpanProcessor(
                    SimpleSpanProcessor.create(LoggingSpanExporter.create())
                );
                logger.info("TraceAI console exporter enabled");
            }

            ownedTracerProvider = providerBuilder.build();
            Tracer tracer = ownedTracerProvider.get(TRACER_NAME, getVersion());
            instance = new FITracer(tracer, config);
            initialized = true;

            logger.info("TraceAI initialized successfully (version {})", getVersion());
        }
    }

    /**
     * Initializes TraceAI using an application-owned OpenTelemetry instance.
     *
     * <p>For a Java agent or globally registered SDK, pass
     * {@code GlobalOpenTelemetry.get()}. No provider, exporter, processor or shutdown
     * hook is created. The caller retains responsibility for configuring export,
     * resource attributes, flushing and shutdown. Only FITracer options such as
     * input/output and error-detail hiding are used from {@code config}; export and
     * resource options are ignored. This method does not change global OpenTelemetry.</p>
     *
     * <p>Like {@link #init(TraceConfig)}, the first initialization wins until
     * {@link #shutdown()}. Shutdown only detaches a borrowed instance; it never
     * flushes or closes the caller's provider.</p>
     *
     * @param config the FITracer configuration
     * @param openTelemetry the caller-owned OpenTelemetry instance
     * @throws IllegalArgumentException if either argument is null
     */
    public static void init(TraceConfig config, OpenTelemetry openTelemetry) {
        if (config == null || openTelemetry == null) {
            throw new IllegalArgumentException("TraceConfig and OpenTelemetry cannot be null");
        }
        synchronized (lock) {
            if (initialized) {
                logger.debug("TraceAI already initialized, skipping");
                return;
            }
            instance = new FITracer(openTelemetry.getTracer(TRACER_NAME, getVersion()), config);
            initialized = true;
            logger.info("TraceAI initialized with application-owned OpenTelemetry (version {})", getVersion());
        }
    }

    /**
     * Initializes TraceAI with configuration from environment variables.
     *
     * <p>Environment variables:</p>
     * <ul>
     *   <li>FI_BASE_URL - The base URL for the backend</li>
     *   <li>FI_API_KEY - The API key for authentication</li>
     *   <li>FI_PROJECT_NAME - The project name</li>
     * </ul>
     */
    public static void initFromEnvironment() {
        init(TraceConfig.fromEnvironment());
    }

    /**
     * Gets the FITracer instance.
     *
     * @return the FITracer instance
     * @throws IllegalStateException if TraceAI has not been initialized
     */
    public static FITracer getTracer() {
        synchronized (lock) {
            if (!initialized) {
                throw new IllegalStateException(
                    "TraceAI not initialized. Call TraceAI.init() or TraceAI.initFromEnvironment() first."
                );
            }
            return instance;
        }
    }

    /**
     * Checks if TraceAI has been initialized.
     *
     * @return true if initialized, false otherwise
     */
    public static boolean isInitialized() {
        synchronized (lock) {
            return initialized;
        }
    }

    /**
     * Flushes and closes a TraceAI-owned provider, or detaches a borrowed instance.
     * Does not change global OpenTelemetry or flush/close an application-owned SDK.
     * Subsequent initialization is allowed; obtain a new FITracer after reinitializing.
     */
    public static void shutdown() {
        synchronized (lock) {
            if (ownedTracerProvider != null) {
                ownedTracerProvider.forceFlush().join(10, TimeUnit.SECONDS);
                ownedTracerProvider.shutdown().join(10, TimeUnit.SECONDS);
                ownedTracerProvider = null;
            }
            instance = null;
            initialized = false;
        }
    }

    /**
     * Gets the version of the TraceAI Java SDK.
     *
     * @return the version string
     */
    public static String getVersion() {
        try (InputStream stream = TraceAI.class.getResourceAsStream("/traceai-version.properties")) {
            if (stream != null) {
                Properties properties = new Properties();
                properties.load(stream);
                return properties.getProperty("version", "unknown");
            }
        } catch (IOException e) {
            logger.debug("Unable to read TraceAI version", e);
        }
        return "unknown";
    }

    // Private constructor to prevent instantiation
    private TraceAI() {
        throw new UnsupportedOperationException("Utility class cannot be instantiated");
    }
}
