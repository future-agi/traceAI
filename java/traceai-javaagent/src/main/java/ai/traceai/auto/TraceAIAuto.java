package ai.traceai.auto;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.context.ContextKey;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import net.bytebuddy.agent.ByteBuddyAgent;
import java.lang.instrument.Instrumentation;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** Zero-code agent and optional one-line initialization. All entry points fail open. */
public final class TraceAIAuto {
    public static final ContextKey<Boolean> SUPPRESS = ContextKey.named("traceai.auto.suppress");
    static final ThreadLocal<Boolean> IN_FLIGHT = ThreadLocal.withInitial(() -> false);
    private static final AtomicBoolean WARNED = new AtomicBoolean();
    private static volatile boolean active, initialized;
    private static final AtomicBoolean REPEATED = new AtomicBoolean();
    private static volatile AutoConfig config;
    static final class RuntimeState {
        private final AutoConfig config;
        private final SdkTracerProvider provider;
        private final Tracer tracer;
        private final OtlpJsonExporter exporter;
        final BatchSpanProcessor processor;
        final Resource resource;
        private boolean closing, closeRequested;
        private int finalizing;
        final io.opentelemetry.sdk.common.CompletableResultCode termination = new io.opentelemetry.sdk.common.CompletableResultCode();
        RuntimeState(AutoConfig config, SdkTracerProvider provider, Tracer tracer, OtlpJsonExporter exporter) { this(config, provider, tracer, exporter, null); }
        RuntimeState(AutoConfig config, SdkTracerProvider provider, Tracer tracer, OtlpJsonExporter exporter, BatchSpanProcessor processor) { this(config, provider, tracer, exporter, processor, resource(config)); }
        RuntimeState(AutoConfig config, SdkTracerProvider provider, Tracer tracer, OtlpJsonExporter exporter, BatchSpanProcessor processor, Resource resource) {
            this.config = config; this.provider = provider; this.tracer = tracer; this.exporter = exporter; this.processor = processor; this.resource = resource;
        }
        AutoConfig config() { return config; }
        SdkTracerProvider provider() { return provider; }
        Tracer tracer() { return tracer; }
        OtlpJsonExporter exporter() { return exporter; }
        synchronized boolean acceptingResults() { return !closing; }
        synchronized boolean startFinalizing() { if (closing) return false; finalizing++; return true; }
        synchronized void endFinalizing() { finalizing--; notifyAll(); }
        synchronized void close(java.time.Duration timeout, boolean wait) {
            if (closeRequested) return;
            closeRequested = true;
            Lifecycle.cleanup(() -> {
                try {
                    // Drain already-observed completions, not unconsumed/in-flight bodies.
                    // The caller's deadline is enforced by Lifecycle; this worker is a daemon.
                    synchronized (this) { while (finalizing > 0) this.wait(); closing = true; }
                    var result = provider.shutdown();
                    result.whenComplete(() -> { if (result.isSuccess()) termination.succeed(); else termination.fail(); });
                    return result;
                } catch (Throwable ignored) { termination.fail(); return termination; }
            }, timeout, wait);
        }
    }
    private static final java.util.concurrent.atomic.AtomicReference<RuntimeState> RUNTIME = new java.util.concurrent.atomic.AtomicReference<>();
    private static final Map<String, String> STATUS = new java.util.concurrent.ConcurrentHashMap<>();

    // Object keeps the public descriptor identical before and after shading the private SDK.
    public static Object init() { return init(Map.of()); }
    public static Object init(Map<String, ?> options) {
        try { start(options, null); } catch (Throwable ignored) { warn("Initialization failed; application runs without auto-instrumentation"); }
        RuntimeState state = RUNTIME.get(); return state == null ? null : state.provider();
    }
    static synchronized void start(Map<String, ?> options, Instrumentation instrumentation) {
        if (initialized) {
            if (REPEATED.compareAndSet(false, true)) System.err.println("[traceai-auto] Already initialized; existing provider and configuration retained");
            return;
        }
        initialized = true;
        try {
            config = AutoConfig.resolve(options);
            KnownSecrets.PROCESS.seed(config, System.getenv());
            reportLibraries();
            if (config.flag("OTEL_SDK_DISABLED")) { STATUS.put("wire", "disabled"); return; }
            if (!config.credentials()) { STATUS.put("wire", "disabled (missing credentials)"); warn("FI_API_KEY and FI_SECRET_KEY are required; auto-instrumentation is off"); return; }
            OtlpJsonExporter exporter = new OtlpJsonExporter(config);
            BatchSpanProcessor processor = BatchSpanProcessor.builder(exporter).setScheduleDelay(200, TimeUnit.MILLISECONDS).setExporterTimeout(3, TimeUnit.SECONDS).build();
            Resource resource = resource(config);
            SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(Sampler.parentBased(Sampler.traceIdRatioBased(config.sampleRate)))
                .setSpanLimits(io.opentelemetry.sdk.trace.SpanLimits.builder().setMaxNumberOfAttributes(attributeLimit(config)).build())
                .setResource(resource)
                .addSpanProcessor(new WireResourceProcessor(processor)).build();
            OpenTelemetrySdk sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
            try { GlobalOpenTelemetry.set(sdk); } catch (IllegalStateException ignored) { }
            // Application SDK processors are bridged by advice, without taking ownership of their provider.
            RUNTIME.set(new RuntimeState(config, provider, sdk.getTracer("traceai-java-auto", "1.0.0"), exporter, processor, resource));
            if (instrumentation == null) instrumentation = ByteBuddyAgent.install();
            STATUS.put("java.net.http", transportEnabled("java.net.http") ? "pending (enabled when loaded)" : "disabled");
            STATUS.put("okhttp", !transportEnabled("okhttp") ? "disabled" : present("okhttp3.OkHttpClient") ? "pending (enabled when loaded)" : "not_installed");
            TraceAIAgent.install(instrumentation);
            active = true;
            STATUS.put("wire", config.enabled("wire") && config.flag("FI_WIRE_CAPTURE") ? "instrumented" : "disabled");
            Runtime.getRuntime().addShutdownHook(new Thread(TraceAIAuto::shutdown, "traceai-auto-shutdown"));
        } catch (Throwable ignored) {
            active = false; RuntimeState failed = RUNTIME.getAndSet(null);
            if (failed != null) failed.close(java.time.Duration.ofSeconds(5), false);
            STATUS.put("wire", "failed"); warn("Initialization failed; application runs without auto-instrumentation");
        }
        finally { if (config != null && config.flag("FI_AUTO_DEBUG")) System.err.println(doctor()); }
    }
    public static String doctor() {
        try {
            AutoConfig resolved = config != null ? config : AutoConfig.resolve(Map.of());
            if (config == null) { config = resolved; reportLibraries(); }
            RuntimeState state = RUNTIME.get();
            return WireCore.dump(Map.of("active", active, "config", resolved.masked(), "integrations", new TreeMap<>(STATUS), "collector", state == null ? "not_tested (agent inactive)" : state.exporter().lastExport,
                "otel", "isolated shaded SDK; application context and span processors bridged; existing provider unchanged",
                "attribute_projection", Map.of("zero_capacity_spans", AttributeProjection.ZERO_CAPACITY.get(), "protected_overflow_spans", AttributeProjection.PROTECTED_OVERFLOW.get())));
        } catch (Throwable ignored) { return "{\"status\":\"unavailable\"}"; }
    }
    public static String doctor(boolean checkCollector) {
        if (checkCollector) {
            try {
                RuntimeState state = RUNTIME.get();
                if (state != null && active) {
                    Span test = state.tracer().spanBuilder("traceai.auto.doctor").startSpan();
                    test.setAttribute("traceai.auto.diagnostic", true); test.end(); flush();
                }
            } catch (Throwable ignored) { }
        }
        return doctor();
    }
    /** Registers an integration-observed credential (8+ characters) for all subsequent exports. */
    public static void registerSecret(String value) {
        try { KnownSecrets.PROCESS.register(value); } catch (Throwable ignored) { KnownSecrets.PROCESS.failClosed(); }
    }
    public static AutoCloseable suppress() {
        try { return io.opentelemetry.context.Context.current().with(SUPPRESS, true).makeCurrent(); }
        catch (Throwable ignored) { return () -> {}; }
    }
    public static void main(String[] args) {
        boolean check = Arrays.asList(args).contains("--check");
        if (check) init();
        // Default doctor/bootstrap only lists; --check explicitly requests a diagnostic export.
        System.out.println(doctor(check));
    }
    public static void flush() { flush(java.time.Duration.ofSeconds(5)); }
    public static void flush(java.time.Duration timeout) {
        RuntimeState state = RUNTIME.get();
        if (state != null) Lifecycle.run(() -> state.provider().forceFlush(), timeout, true);
    }
    public static void shutdown() { shutdown(java.time.Duration.ofSeconds(5)); }
    public static void shutdown(java.time.Duration timeout) {
        active = false; RuntimeState state = RUNTIME.get();
        if (state == null) return;
        state.close(java.time.Duration.ZERO, false);
        Lifecycle.run(() -> state.termination, timeout, true);
    }
    static int attributeLimit(AutoConfig config) {
        for (String key : List.of("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "OTEL_ATTRIBUTE_COUNT_LIMIT")) {
            String value = config.values.get(key);
            if (value != null) try { int count = Integer.parseInt(value.trim()); if (count >= 0) return count; } catch (NumberFormatException ignored) { }
        }
        return 128;
    }
    static Resource resource(AutoConfig config) {
        var environment = Attributes.builder();
        for (String part : config.values.getOrDefault("OTEL_RESOURCE_ATTRIBUTES", "").split(",")) {
            String[] pair = part.split("=", 2);
            if (pair.length == 2 && !pair[0].trim().isEmpty()) try {
                environment.put(pair[0].trim(), java.net.URLDecoder.decode(pair[1].trim().replace("+", "%2B"), java.nio.charset.StandardCharsets.UTF_8));
            } catch (IllegalArgumentException ignored) { }
        }
        String service = config.values.get("OTEL_SERVICE_NAME");
        if (service != null && !service.isBlank()) environment.put("service.name", service);
        Resource resource = Resource.getDefault().merge(Resource.create(environment.build())).merge(Resource.create(Attributes.builder()
            .put("project_name", config.get("FI_PROJECT_NAME")).put("project_type", config.get("FI_PROJECT_TYPE"))
            .put("project_version_name", config.get("FI_PROJECT_VERSION_NAME"))
            .put("project_version_id", config.values.getOrDefault("FI_PROJECT_VERSION_ID", UUID.randomUUID().toString()))
            .put("eval_tags", config.get("FI_EVAL_TAGS")).put("metadata", config.get("FI_METADATA"))
            .put("semantic_convention", "fi").build()));
        Resource host = ApplicationOtel.resource(null, Thread.currentThread().getContextClassLoader());
        return host == null ? resource : resource.merge(host);
    }
    static Resource withRouting(Resource host, Resource agent) {
        var routing = Attributes.builder();
        agent.getAttributes().forEach((key, value) -> {
            String name = key.getKey();
            if (name.startsWith("project_") || name.equals("eval_tags") || name.equals("metadata") || name.equals("semantic_convention")) copyAttribute(routing, key, value);
        });
        return Resource.getDefault().merge(Resource.create(routing.build())).merge(host);
    }
    @SuppressWarnings({"rawtypes", "unchecked"})
    private static void copyAttribute(io.opentelemetry.api.common.AttributesBuilder target, AttributeKey key, Object value) { target.put(key, value); }
    static void warn(String text) { if (WARNED.compareAndSet(false, true)) System.err.println("[traceai-auto] " + text); }
    static void debug(String text) { if (config != null && config.flag("FI_AUTO_DEBUG")) System.err.println("[traceai-auto] " + text); }
    static boolean transportEnabled(String name) { return config != null && config.enabled("wire") && config.flag("FI_WIRE_CAPTURE") && !AutoConfig.names(config.get("FI_DISABLED_INSTRUMENTATIONS")).contains(name); }
    static void integrationInstrumented(String name) { STATUS.put(name, transportEnabled(name) ? "instrumented" : "disabled"); }
    static void integrationFailed(String name) { STATUS.put(name, "failed"); warn("An integration could not be enabled; application requests are unaffected"); }
    static AutoConfig config() { return config; }
    static RuntimeState currentRuntime() { return active ? RUNTIME.get() : null; }
    public static Object applicationProcessor(Object original) { return ApplicationOtel.processor(original); }
    static WireCapture begin(String method, String url, Map<String, String> headers) { return begin(method, url, headers, null); }
    static WireCapture begin(String method, String url, Map<String, String> headers, OtelBridge.Snapshot requestContext) {
        try {
            if (!active) return null;
            KnownSecrets.observe(headers, url);
            OtelBridge.Snapshot context = requestContext == null ? OtelBridge.snapshot() : requestContext;
            if (IN_FLIGHT.get()) return null;
            WireCore.Match match = WireCore.match(method, url); if (match == null || context.suppressed()) return null;
            RuntimeState state = RUNTIME.get();
            if (state == null || !state.config().enabled("wire") || !state.config().flag("FI_WIRE_CAPTURE") || !state.acceptingResults()) return null;
            return new WireCapture(new WireCore.Request(method, url), headers, match, context, state.config().maxBodyBytes, state);
        } catch (Throwable ignored) { return null; }
    }
    static void emit(WireCapture capture, WireCore.Result result) {
        try {
            RuntimeState state = capture.runtime;
            if (state == null || !state.acceptingResults() || result == null) return;
            // A7 sanitizes the complete candidate context before bounded admission; S1 at
            // asynchronous export remains authoritative (including newly observed secrets).
            Resource resource = capture.context.resource() == null ? state.resource : withRouting(capture.context.resource(), state.resource);
            Span span = state.tracer().spanBuilder(bounded(result.name())).setSpanKind(SpanKind.CLIENT).setParent(capture.context.parent())
                .setStartTimestamp(capture.startEpochNanos, TimeUnit.NANOSECONDS).startSpan();
            try {
                Map<String, Object> attrs = new LinkedHashMap<>(capture.context.attributes()); attrs.putAll(result.attributes());
                attrs = AttributeProjection.select(AttributeProjection.sanitize(attrs, state.config(), capture.match),
                    Math.min(state.provider().getSpanLimits().getMaxNumberOfAttributes(), capture.context.attributeLimit() == null ? Integer.MAX_VALUE : capture.context.attributeLimit()),
                    String.valueOf(capture.match.endpoint().get("operation")));
                for (Map.Entry<String, Object> e : attrs.entrySet()) {
                    String k = e.getKey(); Object v = e.getValue();
                    if (hidden(k, capture.match, state.config())) continue;
                    if (v instanceof String) span.setAttribute(k, (String)v);
                    else if (v instanceof Boolean) span.setAttribute(k, (Boolean)v);
                    else if (v instanceof Float || v instanceof Double) span.setAttribute(k, ((Number)v).doubleValue());
                    else if (v instanceof Number) span.setAttribute(k, ((Number)v).longValue());
                    else if (v instanceof List<?> list) span.setAttribute(AttributeKey.stringArrayKey(k), list.stream().map(item -> item instanceof String s ? s : item == null ? "null" : item.getClass().getName()).toList());
                }
                span.setStatus(result.ok() ? StatusCode.OK : StatusCode.ERROR, result.description() == null || hidesContent(state.config()) ? "" : result.description());
            } finally { WireResourceProcessor.end(span, capture.endEpochNanos, resource); }
        } catch (Throwable ignored) { }
    }
    static boolean hidden(String key, WireCore.Match match) { return hidden(key, match, config); }
    static boolean hidden(String key, WireCore.Match match, AutoConfig config) {
        if (config.flag("FI_HIDE_LLM_INVOCATION_PARAMETERS") && (key.equals("gen_ai.request.parameters") || key.equals("llm.invocation_parameters"))) return true;
        if (config.flag("FI_HIDE_INPUT_IMAGES") && (key.toLowerCase(Locale.ROOT).contains("image") || key.toLowerCase(Locale.ROOT).contains("file") && (key.endsWith(".uri") || key.endsWith(".url")) || key.equals("input.value") || key.equals("fi.raw_input"))) return true;
        boolean input = config.masking.isHideInputs(), output = config.masking.isHideOutputs();
        boolean inputText = input || config.flag("FI_HIDE_INPUT_TEXT"), outputText = output || config.flag("FI_HIDE_OUTPUT_TEXT");
        boolean inputMessages = input || config.masking.isHideInputMessages(), outputMessages = output || config.masking.isHideOutputMessages();
        if ((inputText || inputMessages) && (key.equals("fi.raw_input") || key.startsWith("input.") || key.startsWith("gen_ai.input.") || key.equals("gen_ai.system_instructions") || key.startsWith("gen_ai.tool.") || key.equals("gen_ai.request.parameters")
            || key.equals("cohere.preamble") || key.equals("embedding.input_text") || key.startsWith("embedding.input_text.") || key.equals("gen_ai.reranker.query"))) return true;
        // Existing wrappers also emit generated tool calls outside gen_ai.output.messages.
        if ((outputText || outputMessages) && (key.equals("fi.raw_output") || key.startsWith("output.") || key.startsWith("gen_ai.output.") || key.equals("message.tool_calls") || key.startsWith("message.tool_calls."))) return true;
        if ((inputText || inputMessages || outputText || outputMessages) && Set.of("exception.message", "exception.stacktrace", "error.message", "error.stack").contains(key)) return true;
        return key.equals("gen_ai.audio.transcript") && ("text_to_speech".equals(match.endpoint().get("operation")) ? inputText || inputMessages : outputText || outputMessages);
    }
    static boolean hidesContent(AutoConfig config) {
        return config.masking.isHideInputs() || config.masking.isHideOutputs() || config.masking.isHideInputMessages() || config.masking.isHideOutputMessages()
            || config.flag("FI_HIDE_INPUT_TEXT") || config.flag("FI_HIDE_OUTPUT_TEXT") || config.flag("FI_HIDE_INPUT_IMAGES") || config.flag("FI_HIDE_LLM_INVOCATION_PARAMETERS");
    }
    private static String bounded(String value) { return value == null || value.length() <= 32000 ? value : value.substring(0, 31986) + "...[truncated]"; }
    static Object scrub(Object value) { return new WirePrivacy(Map.of(), config).clean(value); }
    private static boolean present(String name) {
        try { return Thread.currentThread().getContextClassLoader().getResource(name.replace('.', '/') + ".class") != null; } catch (Throwable ignored) { return false; }
    }
    private static void reportLibraries() {
        String[][] libraries = {
            {"openai", "com.openai.client.OpenAIClient", "ai.traceai.openai.TracedOpenAIClient"},
            {"anthropic", "com.anthropic.client.AnthropicClient", "ai.traceai.anthropic.TracedAnthropicClient"},
            {"azure-openai", "com.azure.ai.openai.OpenAIClient", "ai.traceai.azure.openai.TracedAzureOpenAIClient"},
            {"cohere", "com.cohere.api.Cohere", "ai.traceai.cohere.TracedCohereClient"},
            {"google-genai", "com.google.genai.Client", "ai.traceai.googlegenai.TracedGenerativeModel"},
            {"ollama", "io.github.ollama4j.OllamaAPI", "ai.traceai.ollama.TracedOllamaAPI"},
            {"bedrock", "software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient", "ai.traceai.bedrock.TracedBedrockRuntimeClient"},
            {"vertexai", "com.google.cloud.vertexai.VertexAI", "ai.traceai.vertexai.TracedGenerativeModel"},
            {"watsonx", "com.ibm.watsonx.ai.chat.ChatService", "ai.traceai.watsonx.TracedWatsonxAI"},
            {"langchain4j", "dev.langchain4j.model.chat.ChatLanguageModel", "ai.traceai.langchain4j.TracedChatLanguageModel"},
            {"spring-ai", "org.springframework.ai.chat.model.ChatModel", "ai.traceai.spring.TracedChatModel"},
            {"semantic-kernel", "com.microsoft.semantickernel.Kernel", "ai.traceai.semantickernel.TracedKernel"},
            {"pinecone", "io.pinecone.clients.Pinecone", "ai.traceai.pinecone.TracedPineconeIndex"},
            {"qdrant", "io.qdrant.client.QdrantClient", "ai.traceai.qdrant.TracedQdrantClient"},
            {"milvus", "io.milvus.client.MilvusServiceClient", "ai.traceai.milvus.TracedMilvusClient"},
            {"weaviate", "io.weaviate.client.WeaviateClient", "ai.traceai.weaviate.TracedWeaviateClient"},
            {"chromadb", "tech.amikos.chromadb.Client", "ai.traceai.chromadb.TracedChromaCollection"},
            {"mongodb", "com.mongodb.client.MongoClient", "ai.traceai.mongodb.TracedMongoVectorSearch"},
            {"redis", "redis.clients.jedis.JedisPooled", "ai.traceai.redis.TracedRedisVectorSearch"},
            {"azure-search", "com.azure.search.documents.SearchClient", "ai.traceai.azure.search.TracedSearchClient"},
            {"pgvector", "com.pgvector.PGvector", "ai.traceai.pgvector.TracedPgVectorStore"},
            {"elasticsearch", "co.elastic.clients.elasticsearch.ElasticsearchClient", "ai.traceai.elasticsearch.TracedElasticsearchClient"}
        };
        for (String[] library : libraries) STATUS.put(library[0], !config.enabled(library[0]) ? "disabled" : !present(library[1]) ? "not_installed" : !present(library[2]) ? "missing_instrumentor (known HTTP protocols still covered by wire)" : "manual (use " + library[2] + "; wrapper/listener registration required)");
        STATUS.put("servlet", "manual (root SERVER spans not yet implemented)");
        boolean wire = config.enabled("wire") && config.flag("FI_WIRE_CAPTURE") && config.credentials();
        STATUS.putIfAbsent("wire", wire ? "pending (agent not initialized)" : "disabled");
        STATUS.putIfAbsent("java.net.http", wire && transportEnabled("java.net.http") ? "pending (enabled when loaded)" : "disabled");
        STATUS.putIfAbsent("okhttp", !wire || !transportEnabled("okhttp") ? "disabled" : present("okhttp3.OkHttpClient") ? "pending (enabled when loaded)" : "not_installed");
    }
    // Called reflectively from advice in the java.net.http module; only JDK types cross that boundary.
    public static void jdkRequestHeader(Object request, String name, String value) { JdkTransport.header(request, name, value); }
    public static void jdkBody(Object stream) { JdkTransport.body(stream); }
    public static void jdkClosed(Object stream) { JdkTransport.closed(stream); }
    public static Object[] enterJdk(Object request, Object handler) { return JdkTransport.enter(request, handler); }
    public static void exitJdk(Object state, Object returned, Throwable error) { JdkTransport.exit(state, returned, error); }
    public static void okhttpSource(Object body, Object source) { try { OkHttpSources.source(body, source); } catch (Throwable ignored) { } }
    public static Object okhttpReading(Object source) { try { return OkHttpSources.reading(source); } catch (Throwable ignored) { return null; } }
    public static void okhttpRead(Object token, Object sink, long count, Throwable error, long observed) { try { OkHttpSources.read(token, sink, count, error, observed); } catch (Throwable ignored) { } }
    public static void okhttpClosed(Object token, Throwable error) { try { OkHttpSources.closed(token, error); } catch (Throwable ignored) { } }
    public static void okhttpRequestHeaders(Object request) {
        try { if (active) OkHttpTransport.observeRequest(request); } catch (Throwable ignored) { }
    }
    public static void requestOkHttp(Object call) {
        try { if (active) OkHttpTransport.requestContext(call); } catch (Throwable ignored) { }
    }
    public static void installOkHttp(Object builder) {
        try { if (transportEnabled("okhttp")) OkHttpTransport.install(builder); } catch (Throwable ignored) { integrationFailed("okhttp"); }
    }
    private TraceAIAuto() {}
}
