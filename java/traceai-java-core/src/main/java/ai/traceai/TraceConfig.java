package ai.traceai;

import io.opentelemetry.sdk.trace.samplers.Sampler;
import java.util.Map;

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
    private boolean hideErrorDetails = false;
    private String baseUrl;
    private String apiKey;
    private String secretKey;
    private String projectName;
    private String serviceName;
    private boolean enableConsoleExporter = false;
    private int batchSize = 512;
    private long exportIntervalMs = 5000;
    private Sampler sampler;

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
     * Resolves sampling for a TraceAI-owned provider. Shared with the shaded auto agent;
     * application-owned providers must not use this to replace their sampler.
     * An explicit FI_SAMPLE_RATE wins; otherwise the six standard OTel sampler names apply.
     * Invalid standard settings produce one value-free warning and the parent-based default.
     *
     * @param environment sampling environment (or resolved auto configuration without synthetic defaults)
     * @return the selected sampler
     */
    public static Sampler samplerFromEnvironment(Map<String, String> environment) {
        String explicitRate = environment.get("FI_SAMPLE_RATE");
        if (explicitRate != null) {
            double rate = 1.0;
            try {
                rate = Double.parseDouble(explicitRate);
                if (!Double.isFinite(rate) || rate < 0 || rate > 1) rate = 1.0;
            } catch (NumberFormatException ignored) { }
            return SamplingRatioSampler.ratioSampler(rate, true);
        }
        String name = environment.get("OTEL_TRACES_SAMPLER");
        if (name == null) return SamplingRatioSampler.ratioSampler(1.0, true);
        try {
            switch (name) {
                case "always_on": return Sampler.alwaysOn();
                case "always_off": return Sampler.alwaysOff();
                case "parentbased_always_on": return SamplingRatioSampler.parentBasedSampler(Sampler.alwaysOn(), 1.0);
                case "parentbased_always_off": return SamplingRatioSampler.parentBasedSampler(Sampler.alwaysOff(), 0.0);
                case "traceidratio":
                case "parentbased_traceidratio":
                    String arg = environment.get("OTEL_TRACES_SAMPLER_ARG");
                    double rate = arg == null || arg.isEmpty() ? 1.0 : Double.parseDouble(arg);
                    if (!Double.isFinite(rate) || rate < 0 || rate > 1) throw new IllegalArgumentException();
                    return SamplingRatioSampler.ratioSampler(rate, name.equals("parentbased_traceidratio"));
                default: throw new IllegalArgumentException();
            }
        } catch (IllegalArgumentException ignored) {
            // Do not echo configuration values: they may contain unrelated private data.
            System.err.println("[traceai] Invalid OTEL_TRACES_SAMPLER or OTEL_TRACES_SAMPLER_ARG; using default ParentBased(TraceIdRatio(1.0))");
            return SamplingRatioSampler.ratioSampler(1.0, true);
        }
    }

    /**
     * Enables sampling-ratio attributes only for a TraceAI-owned provider. Recognizes partial
     * ratio and parent-based samplers returned by {@link #samplerFromEnvironment(Map)}, including
     * an explicit builder sampler obtained from that factory. Parent-based samplers copy valid
     * upstream ratios even at root rates zero or one; all other samplers are returned unchanged.
     * This must not be applied to an application-owned provider.
     *
     * @param sampler the selected sampler
     * @return a decision-preserving sampler carrying ratio metadata when the ratio is known
     */
    public static Sampler samplerForOwnedProvider(Sampler sampler) {
        return SamplingRatioSampler.forOwnedProvider(sampler);
    }

    /**
     * Builder for TraceConfig instances.
     */
    public static class Builder {
        private final TraceConfig config = new TraceConfig();
        private Boolean hideInputs;
        private Boolean hideOutputs;
        private Boolean hideInputMessages;
        private Boolean hideOutputMessages;
        private Boolean hideErrorDetails;

        /**
         * Sets whether to hide input values, raw input and input messages in traces.
         * @param hide true to hide inputs
         * @return this builder
         */
        public Builder hideInputs(boolean hide) {
            hideInputs = hide;
            return this;
        }

        /**
         * Sets whether to hide output values, raw output and output messages in traces.
         * @param hide true to hide outputs
         * @return this builder
         */
        public Builder hideOutputs(boolean hide) {
            hideOutputs = hide;
            return this;
        }

        /**
         * Sets whether to hide input messages in traces.
         * @param hide true to hide input messages
         * @return this builder
         */
        public Builder hideInputMessages(boolean hide) {
            hideInputMessages = hide;
            return this;
        }

        /**
         * Sets whether to hide output messages in traces.
         * @param hide true to hide output messages
         * @return this builder
         */
        public Builder hideOutputMessages(boolean hide) {
            hideOutputMessages = hide;
            return this;
        }

        /**
         * Hides error messages, stack traces and status descriptions recorded by FITracer.
         * Error status and exception/error type are retained. Defaults to
         * FI_HIDE_ERROR_DETAILS, or false when the variable is unset.
         * @param hide true to hide error details, independently of input/output hiding
         * @return this builder
         */
        public Builder hideErrorDetails(boolean hide) {
            hideErrorDetails = hide;
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
         * Sets the sampler used only when TraceAI creates its own provider.
         * This takes precedence over FI_SAMPLE_RATE and OTEL_TRACES_SAMPLER/_ARG.
         * A null or absent option uses those environment settings; it never changes
         * an application-owned provider passed to TraceAI.init(config, openTelemetry).
         * @param sampler the sampler, or null for environment/default selection
         * @return this builder
         */
        public Builder sampler(Sampler sampler) {
            config.sampler = sampler;
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
         * FI_BASE_URL, FI_API_KEY, FI_SECRET_KEY, FI_PROJECT_NAME,
         * FI_HIDE_INPUTS, FI_HIDE_OUTPUTS, FI_HIDE_INPUT_MESSAGES,
         * FI_HIDE_OUTPUT_MESSAGES, FI_HIDE_ERROR_DETAILS.
         * Explicit boolean values (including false) win.
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
            config.hideInputs = hideInputs != null
                ? hideInputs : Boolean.parseBoolean(System.getenv("FI_HIDE_INPUTS"));
            config.hideOutputs = hideOutputs != null
                ? hideOutputs : Boolean.parseBoolean(System.getenv("FI_HIDE_OUTPUTS"));
            config.hideInputMessages = hideInputMessages != null
                ? hideInputMessages : Boolean.parseBoolean(System.getenv("FI_HIDE_INPUT_MESSAGES"));
            config.hideOutputMessages = hideOutputMessages != null
                ? hideOutputMessages : Boolean.parseBoolean(System.getenv("FI_HIDE_OUTPUT_MESSAGES"));
            config.hideErrorDetails = hideErrorDetails != null
                ? hideErrorDetails : Boolean.parseBoolean(System.getenv("FI_HIDE_ERROR_DETAILS"));
            return config;
        }
    }

    // Getters
    public boolean isHideInputs() { return hideInputs; }
    public boolean isHideOutputs() { return hideOutputs; }
    public boolean isHideInputMessages() { return hideInputMessages; }
    public boolean isHideOutputMessages() { return hideOutputMessages; }
    public boolean isHideErrorDetails() { return hideErrorDetails; }
    public String getBaseUrl() { return baseUrl; }
    public String getApiKey() { return apiKey; }
    public String getSecretKey() { return secretKey; }
    public String getProjectName() { return projectName; }
    public String getServiceName() { return serviceName; }
    public boolean isEnableConsoleExporter() { return enableConsoleExporter; }
    public int getBatchSize() { return batchSize; }
    public long getExportIntervalMs() { return exportIntervalMs; }
    /** @return the explicit owned-provider sampler, or null for environment/default selection */
    public Sampler getSampler() { return sampler; }
}
