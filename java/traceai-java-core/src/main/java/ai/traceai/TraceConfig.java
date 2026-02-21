package ai.traceai;

/**
 * Configuration options for TraceAI tracing.
 * Use the builder pattern to create instances.
 *
 * <pre>
 * TraceConfig config = TraceConfig.builder()
 *     .baseUrl("https://api.futureagi.com")
 *     .apiKey("your-api-key")
 *     .projectName("my-project")
 *     .hideInputs(false)
 *     .hideOutputs(false)
 *     .build();
 * </pre>
 */
public class TraceConfig {

    private boolean hideInputs = false;
    private boolean hideOutputs = false;
    private boolean hideInputMessages = false;
    private boolean hideOutputMessages = false;
    private String baseUrl;
    private String apiKey;
    private String secretKey;
    private String projectName;
    private String serviceName;
    private boolean enableConsoleExporter = false;
    private int batchSize = 512;
    private long exportIntervalMs = 5000;

    /**
     * Creates a new builder for TraceConfig.
     * @return a new builder instance
     */
    public static Builder builder() {
        return new Builder();
    }

    /**
     * Creates a default TraceConfig with values from environment variables.
     * @return a TraceConfig with environment-based defaults
     */
    public static TraceConfig fromEnvironment() {
        return builder()
            .baseUrl(System.getenv("FI_BASE_URL"))
            .apiKey(System.getenv("FI_API_KEY"))
            .secretKey(System.getenv("FI_SECRET_KEY"))
            .projectName(System.getenv("FI_PROJECT_NAME"))
            .build();
    }

    /**
     * Builder for TraceConfig instances.
     */
    public static class Builder {
        private final TraceConfig config = new TraceConfig();

        /**
         * Sets whether to hide input values in traces.
         * @param hide true to hide inputs
         * @return this builder
         */
        public Builder hideInputs(boolean hide) {
            config.hideInputs = hide;
            return this;
        }

        /**
         * Sets whether to hide output values in traces.
         * @param hide true to hide outputs
         * @return this builder
         */
        public Builder hideOutputs(boolean hide) {
            config.hideOutputs = hide;
            return this;
        }

        /**
         * Sets whether to hide input messages in traces.
         * @param hide true to hide input messages
         * @return this builder
         */
        public Builder hideInputMessages(boolean hide) {
            config.hideInputMessages = hide;
            return this;
        }

        /**
         * Sets whether to hide output messages in traces.
         * @param hide true to hide output messages
         * @return this builder
         */
        public Builder hideOutputMessages(boolean hide) {
            config.hideOutputMessages = hide;
            return this;
        }

        /**
         * Sets the base URL for the TraceAI backend.
         * @param url the base URL (e.g., "https://api.futureagi.com")
         * @return this builder
         */
        public Builder baseUrl(String url) {
            config.baseUrl = url;
            return this;
        }

        /**
         * Sets the API key for authentication.
         * @param key the API key
         * @return this builder
         */
        public Builder apiKey(String key) {
            config.apiKey = key;
            return this;
        }

        /**
         * Sets the secret key for authentication.
         * @param key the secret key
         * @return this builder
         */
        public Builder secretKey(String key) {
            config.secretKey = key;
            return this;
        }

        /**
         * Sets the project name for trace attribution.
         * @param name the project name
         * @return this builder
         */
        public Builder projectName(String name) {
            config.projectName = name;
            return this;
        }

        /**
         * Sets the service name for trace attribution.
         * @param name the service name
         * @return this builder
         */
        public Builder serviceName(String name) {
            config.serviceName = name;
            return this;
        }

        /**
         * Enables console exporter for debugging.
         * @param enable true to enable console output
         * @return this builder
         */
        public Builder enableConsoleExporter(boolean enable) {
            config.enableConsoleExporter = enable;
            return this;
        }

        /**
         * Sets the batch size for span export.
         * @param size the batch size
         * @return this builder
         */
        public Builder batchSize(int size) {
            config.batchSize = size;
            return this;
        }

        /**
         * Sets the export interval in milliseconds.
         * @param intervalMs the interval in milliseconds
         * @return this builder
         */
        public Builder exportIntervalMs(long intervalMs) {
            config.exportIntervalMs = intervalMs;
            return this;
        }

        /**
         * Builds the TraceConfig instance.
         * Fields not explicitly set will fall back to environment variables:
         * FI_BASE_URL, FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME.
         * @return the configured TraceConfig
         */
        public TraceConfig build() {
            if (config.baseUrl == null) {
                config.baseUrl = System.getenv("FI_BASE_URL");
            }
            if (config.apiKey == null) {
                config.apiKey = System.getenv("FI_API_KEY");
            }
            if (config.secretKey == null) {
                config.secretKey = System.getenv("FI_SECRET_KEY");
            }
            if (config.projectName == null) {
                config.projectName = System.getenv("FI_PROJECT_NAME");
            }
            return config;
        }
    }

    // Getters
    public boolean isHideInputs() { return hideInputs; }
    public boolean isHideOutputs() { return hideOutputs; }
    public boolean isHideInputMessages() { return hideInputMessages; }
    public boolean isHideOutputMessages() { return hideOutputMessages; }
    public String getBaseUrl() { return baseUrl; }
    public String getApiKey() { return apiKey; }
    public String getSecretKey() { return secretKey; }
    public String getProjectName() { return projectName; }
    public String getServiceName() { return serviceName; }
    public boolean isEnableConsoleExporter() { return enableConsoleExporter; }
    public int getBatchSize() { return batchSize; }
    public long getExportIntervalMs() { return exportIntervalMs; }
}
