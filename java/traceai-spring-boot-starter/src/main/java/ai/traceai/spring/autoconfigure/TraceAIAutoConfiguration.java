package ai.traceai.spring.autoconfigure;

import ai.traceai.FITracer;
import ai.traceai.TraceAI;
import ai.traceai.KeyTransport;
import ai.traceai.TraceConfig;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;

/**
 * Auto-configuration for TraceAI in Spring Boot applications.
 *
 * <p>This auto-configuration is enabled by default when the traceai-spring-boot-starter
 * is on the classpath. It can be disabled by setting {@code traceai.enabled=false}.</p>
 *
 * <p>Configuration can be provided via application properties:</p>
 * <pre>
 * traceai:
 *   base-url: https://api.futureagi.com
 *   api-key: ${TRACEAI_API_KEY}
 *   project-name: my-spring-app
 * </pre>
 */
@AutoConfiguration
@EnableConfigurationProperties(TraceAIProperties.class)
@ConditionalOnProperty(prefix = "traceai", name = "enabled", havingValue = "true", matchIfMissing = true)
public class TraceAIAutoConfiguration {

    private static final Logger logger = LoggerFactory.getLogger(TraceAIAutoConfiguration.class);

    @Value("${spring.application.name:spring-app}")
    private String applicationName;

    /**
     * Creates the FITracer bean with configuration from properties.
     *
     * @param properties the TraceAI configuration properties
     * @return the configured FITracer
     */
    @Bean
    @ConditionalOnMissingBean
    public FITracer fiTracer(TraceAIProperties properties) {
        logger.info("Initializing TraceAI with project: {}", properties.getProjectName());

        // Build configuration from properties
        String serviceName = properties.getServiceName() != null
            ? properties.getServiceName()
            : applicationName;

        TraceConfig config = configuration(properties, serviceName, true);
        // Initialize TraceAI. A refused key transport throws KeyTransport.Refused, whose message names no value; the failure-mode property
        // only chooses whether that fails the startup or disables export. A refused target is never sent to.
        try {
            TraceAI.init(config);
        } catch (KeyTransport.Refused refused) {
            if (properties.isFailOnRefusedTransport()) throw refused;
            logger.warn("TraceAI export is disabled: {}", refused.getMessage());
            TraceAI.init(configuration(properties, serviceName, false));
        }

        logger.info("TraceAI initialized successfully (version {})", TraceAI.getVersion());

        return TraceAI.getTracer();
    }

    private static TraceConfig configuration(TraceAIProperties properties, String serviceName, boolean export) {
        return TraceConfig.builder()
            .baseUrl(export ? properties.getBaseUrl() : null)
            .apiKey(export ? properties.getApiKey() : null)
            .secretKey(export ? properties.getSecretKey() : null)
            .allowInsecureHttp(properties.isAllowInsecureHttp())
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
    }
}
