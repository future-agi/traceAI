package ai.traceai.spring.autoconfigure;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.trace.TracerProvider;
import io.opentelemetry.context.propagation.ContextPropagators;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.DisposableBean;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;

import java.util.ArrayList;
import java.util.List;

/**
 * Spring Boot configuration for {@link FITracer}.
 *
 * <p>This configures TraceAI; it does not instrument Spring AI or any other framework.
 * Add the integration for the library you call, or use that framework's native
 * observability.</p>
 *
 * <p>Provider selection:</p>
 * <ul>
 *   <li>When {@code traceai.base-url} and {@code traceai.api-key} (or {@code FI_BASE_URL}
 *   and {@code FI_API_KEY}) are set, or the console exporter is enabled, TraceAI owns a
 *   provider with that export configuration, as in earlier releases. It never registers
 *   that provider globally.</li>
 *   <li>Otherwise, if the application exposes an {@link OpenTelemetry} or
 *   {@link TracerProvider} bean (for example Spring Boot's native tracing), FITracer
 *   spans use that application-owned pipeline. The starter adds no provider, exporter or
 *   processor and never flushes or closes the application's SDK.</li>
 * </ul>
 *
 * <p>Disable the starter with {@code traceai.enabled=false}.</p>
 * <pre>
 * traceai:
 *   base-url: https://api.futureagi.com
 *   api-key: ${FI_API_KEY}
 *   project-name: my-spring-app
 * </pre>
 */
@AutoConfiguration(afterName = {
    "org.springframework.boot.actuate.autoconfigure.opentelemetry.OpenTelemetryAutoConfiguration",
    "org.springframework.boot.actuate.autoconfigure.tracing.OpenTelemetryAutoConfiguration",
    "org.springframework.boot.actuate.autoconfigure.tracing.OpenTelemetryTracingAutoConfiguration",
    "org.springframework.boot.opentelemetry.autoconfigure.OpenTelemetryAutoConfiguration",
    "org.springframework.boot.micrometer.tracing.opentelemetry.autoconfigure.OpenTelemetryTracingAutoConfiguration"
})
@EnableConfigurationProperties(TraceAIProperties.class)
@ConditionalOnProperty(prefix = "traceai", name = "enabled", havingValue = "true", matchIfMissing = true)
public class TraceAIAutoConfiguration implements DisposableBean {

    private static final Logger logger = LoggerFactory.getLogger(TraceAIAutoConfiguration.class);

    @Value("${spring.application.name:spring-app}")
    private String applicationName;

    // Field injection keeps the existing public no-argument constructor.
    @Autowired
    private ObjectProvider<OpenTelemetry> openTelemetryBeans;

    @Autowired
    private ObjectProvider<TracerProvider> tracerProviderBeans;

    private FITracer initializedTracer;

    /**
     * Creates the FITracer bean with configuration from properties.
     *
     * @param properties the TraceAI configuration properties
     * @return the configured FITracer
     */
    @Bean
    @ConditionalOnMissingBean
    public FITracer fiTracer(TraceAIProperties properties) {
        // An application that initialized the public singleton retains its lifecycle.
        if (TraceAI.isInitialized()) {
            return TraceAI.getTracer();
        }

        String serviceName = properties.getServiceName() != null
            ? properties.getServiceName()
            : applicationName;

        TraceConfig config = TraceConfig.builder()
            .baseUrl(properties.getBaseUrl())
            .apiKey(properties.getApiKey())
            .secretKey(properties.getSecretKey())
            .projectName(properties.getProjectName())
            .serviceName(serviceName)
            .hideInputs(properties.isHideInputs())
            .hideOutputs(properties.isHideOutputs())
            .hideInputMessages(properties.isHideInputMessages())
            .hideOutputMessages(properties.isHideOutputMessages())
            .enableConsoleExporter(properties.isEnableConsoleExporter())
            .batchSize(properties.getBatchSize())
            .exportIntervalMs(properties.getExportIntervalMs())
            .build();

        boolean starterExports = (config.getBaseUrl() != null && config.getApiKey() != null)
            || config.isEnableConsoleExporter();
        OpenTelemetry applicationSdk = starterExports ? null : applicationOpenTelemetry();
        if (applicationSdk != null) {
            warnAboutIgnoredExportSettings(properties, config);
            TraceAI.init(config, applicationSdk);
            logger.info("TraceAI uses the application's OpenTelemetry; export and resources come from that SDK");
        } else {
            logger.info("Initializing TraceAI with project: {}", config.getProjectName());
            TraceAI.init(config);
        }
        initializedTracer = TraceAI.getTracer();
        return initializedTracer;
    }

    private OpenTelemetry applicationOpenTelemetry() {
        OpenTelemetry sdk = openTelemetryBeans != null ? openTelemetryBeans.getIfAvailable() : null;
        if (sdk != null) {
            return sdk;
        }
        TracerProvider provider = tracerProviderBeans != null ? tracerProviderBeans.getIfAvailable() : null;
        if (provider == null) {
            return null;
        }
        // Adapt the API only: never create or assume ownership of another SDK/provider.
        return new OpenTelemetry() {
            @Override
            public TracerProvider getTracerProvider() {
                return provider;
            }

            @Override
            public ContextPropagators getPropagators() {
                return ContextPropagators.noop();
            }
        };
    }

    private void warnAboutIgnoredExportSettings(TraceAIProperties properties, TraceConfig config) {
        TraceAIProperties defaults = new TraceAIProperties();
        List<String> ignored = new ArrayList<>();
        if (config.getBaseUrl() != null) ignored.add("base-url");
        if (config.getApiKey() != null) ignored.add("api-key");
        if (config.getSecretKey() != null) ignored.add("secret-key");
        if (config.getProjectName() != null) ignored.add("project-name");
        if (properties.getServiceName() != null) ignored.add("service-name");
        if (properties.getBatchSize() != defaults.getBatchSize()) ignored.add("batch-size");
        if (properties.getExportIntervalMs() != defaults.getExportIntervalMs()) ignored.add("export-interval-ms");
        if (!ignored.isEmpty()) {
            logger.warn("TraceAI is using the application's OpenTelemetry because traceai.base-url and "
                + "traceai.api-key are not both set; ignoring traceai {}. Configure export and resource "
                + "attributes on the application's OpenTelemetry SDK, or set both connection properties.",
                String.join(", ", ignored));
        }
    }

    /** Flushes an owned provider, or detaches a borrowed one, when this context closes. */
    @Override
    public void destroy() {
        if (initializedTracer != null && TraceAI.isInitialized() && TraceAI.getTracer() == initializedTracer) {
            TraceAI.shutdown();
        }
    }
}
