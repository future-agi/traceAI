package ai.traceai;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SdkTracerProviderBuilder;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.exporter.logging.LoggingSpanExporter;
import io.opentelemetry.semconv.ResourceAttributes;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;
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
    private static SdkTracerProvider tracerProvider;
    private static boolean initialized = false;
    private static final Object lock = new Object();

    /**
     * Initializes TraceAI with the given configuration.
     * This method is idempotent - calling it multiple times has no effect after the first call.
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

            // Build resource with service name
            String serviceName = config.getServiceName() != null
                ? config.getServiceName()
                : config.getProjectName() != null
                    ? config.getProjectName()
                    : "traceai-java-app";

            var resourceAttrsBuilder = Attributes.builder()
                .put(ResourceAttributes.SERVICE_NAME, serviceName);

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

            tracerProvider = providerBuilder.build();

            // Register globally
            OpenTelemetrySdk sdk = OpenTelemetrySdk.builder()
                .setTracerProvider(tracerProvider)
                .buildAndRegisterGlobal();

            // Create FITracer instance
            Tracer tracer = GlobalOpenTelemetry.getTracer(TRACER_NAME, getVersion());
            instance = new FITracer(tracer, config);
            initialized = true;

            // Register shutdown hook
            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                logger.debug("Shutting down TraceAI");
                shutdown();
            }));

            logger.info("TraceAI initialized successfully (version {})", getVersion());
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
     * Shuts down TraceAI and flushes any pending spans.
     */
    public static void shutdown() {
        synchronized (lock) {
            if (tracerProvider != null) {
                tracerProvider.forceFlush().join(10, TimeUnit.SECONDS);
                tracerProvider.shutdown().join(10, TimeUnit.SECONDS);
                tracerProvider = null;
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
        return "0.1.0";
    }

    // Private constructor to prevent instantiation
    private TraceAI() {
        throw new UnsupportedOperationException("Utility class cannot be instantiated");
    }
}
