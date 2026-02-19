package ai.traceai.spring.autoconfigure;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * Configuration properties for TraceAI Spring Boot integration.
 *
 * <p>Example application.yml:</p>
 * <pre>
 * traceai:
 *   enabled: true
 *   base-url: https://api.futureagi.com
 *   api-key: ${TRACEAI_API_KEY}
 *   project-name: my-spring-app
 *   hide-inputs: false
 *   hide-outputs: false
 * </pre>
 */
@ConfigurationProperties(prefix = "traceai")
public class TraceAIProperties {

    /**
     * Enable or disable TraceAI instrumentation.
     */
    private boolean enabled = true;

    /**
     * The base URL for the TraceAI backend.
     */
    private String baseUrl;

    /**
     * The API key for authentication.
     */
    private String apiKey;

    /**
     * The project name for trace attribution.
     */
    private String projectName;

    /**
     * The service name for trace attribution.
     * If not set, uses the Spring application name.
     */
    private String serviceName;

    /**
     * Whether to hide input values in traces.
     */
    private boolean hideInputs = false;

    /**
     * Whether to hide output values in traces.
     */
    private boolean hideOutputs = false;

    /**
     * Whether to hide input messages in traces.
     */
    private boolean hideInputMessages = false;

    /**
     * Whether to hide output messages in traces.
     */
    private boolean hideOutputMessages = false;

    /**
     * Enable console span exporter for debugging.
     */
    private boolean enableConsoleExporter = false;

    /**
     * Batch size for span export.
     */
    private int batchSize = 512;

    /**
     * Export interval in milliseconds.
     */
    private long exportIntervalMs = 5000;

    // Getters and Setters

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public String getBaseUrl() {
        return baseUrl;
    }

    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public String getApiKey() {
        return apiKey;
    }

    public void setApiKey(String apiKey) {
        this.apiKey = apiKey;
    }

    public String getProjectName() {
        return projectName;
    }

    public void setProjectName(String projectName) {
        this.projectName = projectName;
    }

    public String getServiceName() {
        return serviceName;
    }

    public void setServiceName(String serviceName) {
        this.serviceName = serviceName;
    }

    public boolean isHideInputs() {
        return hideInputs;
    }

    public void setHideInputs(boolean hideInputs) {
        this.hideInputs = hideInputs;
    }

    public boolean isHideOutputs() {
        return hideOutputs;
    }

    public void setHideOutputs(boolean hideOutputs) {
        this.hideOutputs = hideOutputs;
    }

    public boolean isHideInputMessages() {
        return hideInputMessages;
    }

    public void setHideInputMessages(boolean hideInputMessages) {
        this.hideInputMessages = hideInputMessages;
    }

    public boolean isHideOutputMessages() {
        return hideOutputMessages;
    }

    public void setHideOutputMessages(boolean hideOutputMessages) {
        this.hideOutputMessages = hideOutputMessages;
    }

    public boolean isEnableConsoleExporter() {
        return enableConsoleExporter;
    }

    public void setEnableConsoleExporter(boolean enableConsoleExporter) {
        this.enableConsoleExporter = enableConsoleExporter;
    }

    public int getBatchSize() {
        return batchSize;
    }

    public void setBatchSize(int batchSize) {
        this.batchSize = batchSize;
    }

    public long getExportIntervalMs() {
        return exportIntervalMs;
    }

    public void setExportIntervalMs(long exportIntervalMs) {
        this.exportIntervalMs = exportIntervalMs;
    }
}
