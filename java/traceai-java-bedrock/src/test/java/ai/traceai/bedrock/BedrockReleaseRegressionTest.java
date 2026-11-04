package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.reactivestreams.Subscriber;
import org.reactivestreams.Subscription;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.core.document.Document;
import software.amazon.awssdk.core.retry.RetryPolicy;
import software.amazon.awssdk.http.Protocol;
import software.amazon.awssdk.http.nio.netty.NettyNioAsyncHttpClient;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.zip.CRC32;

import static org.assertj.core.api.Assertions.*;

/** Real AWS clients, real signed HTTP requests, synthetic credentials and localhost provider fixtures. */
class BedrockReleaseRegressionTest {
    private static final String MODEL = "amazon.nova-pro-v1:0";
    private static final String CONVERSE = "{\"output\":{\"message\":{\"role\":\"assistant\",\"content\":[{\"text\":\"fixture-output\"}]}},"
        + "\"stopReason\":\"end_turn\",\"usage\":{\"inputTokens\":11,\"outputTokens\":7,\"totalTokens\":18}}";
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private FITracer tracer;
    private HttpServer server;
    private ExecutorService executor;
    private volatile byte[] body;
    private volatile boolean eventStream;
    private volatile boolean holdStream;
    private volatile int status = 200;
    private final CountDownLatch release = new CountDownLatch(1);

    @BeforeEach
    void start() throws Exception {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        configure(TraceConfig.builder().hideInputs(false).hideOutputs(false).hideErrorDetails(true));
        body = CONVERSE.getBytes(StandardCharsets.UTF_8);
        executor = Executors.newCachedThreadPool();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        server.createContext("/", exchange -> {
            try {
                exchange.getRequestBody().readAllBytes();
                exchange.getResponseHeaders().set("content-type", eventStream ? "application/vnd.amazon.eventstream" : "application/json");
                if (status != 200) exchange.getResponseHeaders().set("x-amzn-errortype", "ValidationException");
                exchange.sendResponseHeaders(status, holdStream ? 0 : body.length);
                exchange.getResponseBody().write(body);
                exchange.getResponseBody().flush();
                if (holdStream) release.await(10, TimeUnit.SECONDS);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
            } finally {
                exchange.close();
            }
        });
        server.start();
    }

    @AfterEach
    void stop() {
        release.countDown();
        server.stop(0);
        executor.shutdownNow();
        provider.close();
    }

    private void configure(TraceConfig.Builder config) {
        tracer = new FITracer(provider.get("bedrock-release-test"), config.apiKey("synthetic-fi-key")
            .secretKey("synthetic-fi-secret").projectName("offline").build());
    }

    private URI endpoint() { return URI.create("http://127.0.0.1:" + server.getAddress().getPort()); }

    private BedrockRuntimeClient sync() {
        return BedrockRuntimeClient.builder().endpointOverride(endpoint()).region(Region.US_EAST_1)
            .credentialsProvider(StaticCredentialsProvider.create(AwsBasicCredentials.create("synthetic-key", "synthetic-secret")))
            .overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(10)).retryPolicy(RetryPolicy.none())
                .addExecutionInterceptor(new BedrockRuntimeInterceptor(tracer))).build();
    }

    private BedrockRuntimeAsyncClient async() {
        return BedrockRuntimeAsyncClient.builder().endpointOverride(endpoint()).region(Region.US_EAST_1)
            .credentialsProvider(StaticCredentialsProvider.create(AwsBasicCredentials.create("synthetic-key", "synthetic-secret")))
            .httpClientBuilder(NettyNioAsyncHttpClient.builder().protocol(Protocol.HTTP1_1))
            .overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(10)).retryPolicy(RetryPolicy.none())
                .addExecutionInterceptor(new BedrockRuntimeInterceptor(tracer))).build();
    }

    private ConverseRequest request() {
        return ConverseRequest.builder().modelId(MODEL).messages(Message.builder().role(ConversationRole.USER)
            .content(ContentBlock.builder().text("fixture-input").build()).build())
            .toolConfig(ToolConfiguration.builder().tools(Tool.builder().toolSpec(ToolSpecification.builder()
                .name("private-tool").description("private-tool-description")
                .inputSchema(ToolInputSchema.builder().json(Document.fromMap(Map.of("type", Document.fromString("object")))).build())
                .build()).build()).build()).build();
    }

    private ConverseStreamRequest streamRequest() {
        var input = request();
        return ConverseStreamRequest.builder().modelId(MODEL).messages(input.messages()).toolConfig(input.toolConfig()).build();
    }

    private SpanData only() {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        return exporter.getFinishedSpanItems().get(0);
    }
    private String string(SpanData span, String key) { return span.getAttributes().get(AttributeKey.stringKey(key)); }
    private Long count(SpanData span, String key) { return span.getAttributes().get(AttributeKey.longKey(key)); }

    private void usage(SpanData span, long total) {
        assertThat(count(span, SemanticConventions.LLM_TOKEN_COUNT_PROMPT)).isEqualTo(11);
        assertThat(count(span, SemanticConventions.LLM_TOKEN_COUNT_COMPLETION)).isEqualTo(7);
        assertThat(count(span, SemanticConventions.LLM_TOKEN_COUNT_TOTAL)).isEqualTo(total);
    }

    private void hidden(SpanData span) {
        for (String key : List.of(SemanticConventions.INPUT_VALUE, SemanticConventions.OUTPUT_VALUE,
            SemanticConventions.LLM_INPUT_MESSAGES, SemanticConventions.LLM_OUTPUT_MESSAGES,
            SemanticConventions.RAW_INPUT, SemanticConventions.RAW_OUTPUT, SemanticConventions.GEN_AI_TOOL_DEFINITIONS)) {
            assertThat(string(span, key)).as(key).isNull();
        }
        assertThat(span.toString()).doesNotContain("fixture-input", "fixture-output", "private-tool");
    }

    @ParameterizedTest
    @CsvSource({"titan,false", "titan,true", "nova,false", "nova,true", "meta,false", "meta,true"})
    void nativeInvokeModelNormalizesUsageEvenWithHiddenContent(String family, boolean hide) {
        configure(TraceConfig.builder().hideInputs(hide).hideOutputs(hide));
        String model;
        String input;
        String response;
        switch (family) {
            case "titan":
                model = "amazon.titan-text-express-v1";
                input = "{\"inputText\":\"fixture-input\"}";
                response = "{\"inputTextTokenCount\":11,\"results\":[{\"tokenCount\":7,\"outputText\":\"fixture-output\",\"completionReason\":\"FINISH\"}]}";
                break;
            case "nova":
                model = MODEL;
                input = "{\"messages\":[{\"role\":\"user\",\"content\":[{\"text\":\"fixture-input\"}]}]}";
                response = CONVERSE;
                break;
            default:
                model = "meta.llama3-8b-instruct-v1:0";
                input = "{\"prompt\":\"fixture-input\"}";
                response = "{\"generation\":\"fixture-output\",\"prompt_token_count\":11,\"generation_token_count\":7}";
        }
        body = response.getBytes(StandardCharsets.UTF_8);
        try (var client = sync()) {
            var result = client.invokeModel(InvokeModelRequest.builder().modelId(model).contentType("application/json")
                .body(SdkBytes.fromUtf8String(input)).build());
            assertThat(result.body().asUtf8String()).isEqualTo(response);
        }
        usage(only(), 18);
        if (hide) hidden(only());
        else {
            assertThat(string(only(), SemanticConventions.OUTPUT_VALUE)).isEqualTo("fixture-output");
            if (family.equals("nova")) assertThat(string(only(), SemanticConventions.LLM_OUTPUT_MESSAGES)).contains("fixture-output");
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void converseKeepsCacheCountersWhenSdkSupportsThem(boolean hide) {
        configure(TraceConfig.builder().hideInputs(hide).hideOutputs(hide));
        body = CONVERSE.replace("\"totalTokens\":18", "\"totalTokens\":23,\"cacheReadInputTokens\":3,\"cacheWriteInputTokens\":2")
            .getBytes(StandardCharsets.UTF_8);
        TokenUsage decoded;
        try (var client = sync()) { decoded = client.converse(request()).usage(); }
        boolean supported = decoded.sdkFields().stream().anyMatch(f -> f.memberName().equals("cacheReadInputTokens"));
        assertThat(count(only(), SemanticConventions.GEN_AI_USAGE_CACHE_READ_TOKENS)).isEqualTo(supported ? 3L : null);
        assertThat(count(only(), SemanticConventions.GEN_AI_USAGE_CACHE_WRITE_TOKENS)).isEqualTo(supported ? 2L : null);
        usage(only(), 23); // Preserve the provider total; do not add cache counts to it again.
        if (hide) hidden(only());
    }

    @Test
    void environmentHidingAndExplicitOverridesAreTestedInFreshProcesses() throws Exception {
        for (String value : List.of("true", "false", "TrUe", "unset")) {
            ProcessBuilder builder = new ProcessBuilder(System.getProperty("java.home") + "/bin/java", "-cp",
                System.getProperty("surefire.test.class.path", System.getProperty("java.class.path")),
                EnvironmentProbe.class.getName());
            for (String key : List.of("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_ERROR_DETAILS")) {
                if (value.equals("unset")) builder.environment().remove(key);
                else builder.environment().put(key, value);
            }
            builder.redirectErrorStream(true);
            Process child = builder.start();
            try {
                assertThat(child.waitFor(30, TimeUnit.SECONDS)).isTrue();
                String output = new String(child.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
                assertThat(child.exitValue()).as(output).isZero();
            } finally { child.destroyForcibly(); }
        }
    }

    public static class EnvironmentProbe {
        public static void main(String[] args) throws Exception {
            var test = new BedrockReleaseRegressionTest();
            test.start();
            try {
                boolean env = Boolean.parseBoolean(System.getenv("FI_HIDE_INPUTS"));
                for (TraceConfig.Builder config : List.of(TraceConfig.builder(), TraceConfig.builder()
                    .hideInputs(!env).hideOutputs(!env))) {
                    test.configure(config);
                    boolean hide = test.tracer.getConfig().isHideInputs();
                    assertThat(test.tracer.getConfig().isHideOutputs()).isEqualTo(hide);
                    assertThat(test.tracer.getConfig().isHideErrorDetails()).isEqualTo(env);
                    try (var client = test.sync()) { client.converse(test.request()); }
                    test.usage(test.only(), 18);
                    if (hide) test.hidden(test.only());
                    else assertThat(test.only().getAttributes().toString()).contains("fixture-input", "fixture-output", "private-tool");
                    test.exporter.reset();
                }
                assertThat(TraceConfig.fromEnvironment().isHideInputs()).isEqualTo(env);
                assertThat(TraceConfig.fromEnvironment().isHideOutputs()).isEqualTo(env);
                assertThat(TraceConfig.builder().build().isHideInputs()).isEqualTo(env);
                assertThat(TraceConfig.builder().hideInputs(!env).hideOutputs(!env).build().isHideInputs()).isEqualTo(!env);
            } finally { test.stop(); }
        }
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void realStreamEndsAfterMetadataUnderCallingParent(boolean hide) throws Exception {
        configure(TraceConfig.builder().hideInputs(hide).hideOutputs(hide));
        eventStream = true;
        body = frames(false);
        StringBuilder text = new StringBuilder();
        Span parent = provider.get("application").spanBuilder("parent").startSpan();
        AtomicReference<String> callbackSpan = new AtomicReference<>();
        var handler = ConverseStreamResponseHandler.builder()
            .onResponse(response -> {
                assertThat(exporter.getFinishedSpanItems()).isEmpty();
                callbackSpan.set(Span.current().getSpanContext().getSpanId());
            }).subscriber(event -> {
                assertThat(Span.current().getSpanContext().getSpanId()).isEqualTo(callbackSpan.get());
                if (event instanceof ContentBlockDeltaEvent) text.append(((ContentBlockDeltaEvent) event).delta().text());
            }).build();
        try (var client = async()) {
            CompletableFuture<Void> result;
            try (Scope ignored = parent.makeCurrent()) {
                result = new TracedBedrockRuntimeAsyncClient(client, tracer).converseStream(streamRequest(), handler);
                assertThat(Span.current()).isSameAs(parent);
            }
            result.thenRun(() -> assertThat(exporter.getFinishedSpanItems()).hasSize(1)).get(10, TimeUnit.SECONDS);
            assertThat(text.toString()).isEqualTo("fixture-output");
            assertThat(only().getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(only().getSpanId()).isEqualTo(callbackSpan.get());
            assertThat(only().getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            usage(only(), 18);
            boolean cacheSupported = TokenUsage.builder().build().sdkFields().stream()
                .anyMatch(f -> f.memberName().equals("cacheReadInputTokens"));
            assertThat(count(only(), SemanticConventions.GEN_AI_USAGE_CACHE_READ_TOKENS)).isEqualTo(cacheSupported ? 3L : null);
            assertThat(count(only(), SemanticConventions.GEN_AI_USAGE_CACHE_WRITE_TOKENS)).isEqualTo(cacheSupported ? 2L : null);
            if (hide) hidden(only());
            else assertThat(string(only(), SemanticConventions.OUTPUT_VALUE)).isEqualTo("fixture-output");
        } finally { parent.end(); }
        assertThat(exporter.getFinishedSpanItems()).hasSize(2);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void realStreamAssemblesToolArgumentsWithoutChangingEvents(boolean hide) throws Exception {
        configure(TraceConfig.builder().hideInputs(hide).hideOutputs(hide));
        eventStream = true;
        body = frames(true);
        StringBuilder arguments = new StringBuilder();
        var handler = ConverseStreamResponseHandler.builder().subscriber(event -> {
            if (event instanceof ContentBlockDeltaEvent) {
                arguments.append(((ContentBlockDeltaEvent) event).delta().toolUse().input());
            }
        }).build();
        try (var client = async()) {
            new TracedBedrockRuntimeAsyncClient(client, tracer).converseStream(streamRequest(), handler).get(10, TimeUnit.SECONDS);
        }
        assertThat(arguments.toString()).isEqualTo("{\"city\":\"Paris\"}");
        usage(only(), 18);
        if (hide) hidden(only());
        else {
            var messages = com.google.gson.JsonParser.parseString(string(only(), SemanticConventions.LLM_OUTPUT_MESSAGES)).getAsJsonArray();
            var tool = messages.get(0).getAsJsonObject().getAsJsonArray("content").get(0).getAsJsonObject().getAsJsonObject("toolUse");
            assertThat(tool.get("toolUseId").getAsString()).isEqualTo("tool-id");
            assertThat(tool.get("name").getAsString()).isEqualTo("private-tool");
            assertThat(tool.getAsJsonObject("input").get("city").getAsString()).isEqualTo("Paris");
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"subscription", "future", "timeout"})
    void realStreamCancellationEndsOnceBeforeFutureCallbacks(String mode) throws Exception {
        eventStream = true;
        holdStream = true;
        body = frame("messageStart", "{\"role\":\"assistant\"}");
        CountDownLatch received = new CountDownLatch(1);
        AtomicReference<Subscription> subscription = new AtomicReference<>();
        var handler = ConverseStreamResponseHandler.builder().subscriber(() -> new Subscriber<ConverseStreamOutput>() {
            public void onSubscribe(Subscription value) { subscription.set(value); value.request(1); }
            public void onNext(ConverseStreamOutput value) { received.countDown(); }
            public void onError(Throwable error) { }
            public void onComplete() { }
        }).build();
        try (var client = async()) {
            var result = new TracedBedrockRuntimeAsyncClient(client, tracer).converseStream(streamRequest(), handler);
            assertThat(received.await(10, TimeUnit.SECONDS)).isTrue();
            assertThat(exporter.getFinishedSpanItems()).isEmpty();
            var observed = result.handle((value, error) -> {
                // AWS completes its future normally on subscription cancellation (not future cancellation).
                if (mode.equals("subscription")) assertThat(error).isNull();
                else assertThat(error).isNotNull();
                assertThat(only().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
                return null;
            });
            if (mode.equals("subscription")) subscription.get().cancel();
            else if (mode.equals("future")) assertThat(result.cancel(true)).isTrue();
            else result.orTimeout(10, TimeUnit.MILLISECONDS);
            observed.get(10, TimeUnit.SECONDS);
            assertThat(result.isCancelled()).isEqualTo(mode.equals("future"));
            release.countDown();
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void realStreamProviderAndSubscriberErrorsKeepErrorPrivacy(boolean subscriberFailure) throws Exception {
        eventStream = subscriberFailure;
        body = subscriberFailure ? frames(false) : "{\"message\":\"private-provider-error\"}".getBytes(StandardCharsets.UTF_8);
        status = subscriberFailure ? 200 : 400;
        var handler = ConverseStreamResponseHandler.builder().subscriber(event -> {
            if (subscriberFailure) throw new IllegalStateException("private-subscriber-error");
        }).build();
        try (var client = async()) {
            if (subscriberFailure) {
                // AWS's Consumer subscriber catches user exceptions and cancels; native future succeeds.
                client.converseStream(streamRequest(), handler).get(10, TimeUnit.SECONDS);
                assertThat(exporter.getFinishedSpanItems()).isEmpty();
            }
            var result = new TracedBedrockRuntimeAsyncClient(client, tracer).converseStream(streamRequest(), handler);
            if (subscriberFailure) result.get(10, TimeUnit.SECONDS);
            else assertThatThrownBy(() -> result.get(10, TimeUnit.SECONDS)).hasCauseInstanceOf(ValidationException.class);
        }
        assertThat(only().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(only().toString()).doesNotContain("private-provider-error", "private-subscriber-error");
    }

    @Test
    void realStreamExceptionFrameRetainsPartialOutputAndEndsWithError() throws Exception {
        eventStream = true;
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        bytes.write(frame("messageStart", "{\"role\":\"assistant\"}"));
        bytes.write(frame("contentBlockDelta", "{\"contentBlockIndex\":0,\"delta\":{\"text\":\"partial-output\"}}"));
        bytes.write(frame("modelStreamErrorException", "{\"message\":\"private-stream-error\"}", "exception"));
        body = bytes.toByteArray();
        var handler = ConverseStreamResponseHandler.builder().subscriber(event -> { }).build();
        try (var client = async()) {
            var result = new TracedBedrockRuntimeAsyncClient(client, tracer).converseStream(streamRequest(), handler);
            // The SDK floor maps this wire error to its base exception; newer SDKs use a subtype.
            assertThatThrownBy(() -> result.get(10, TimeUnit.SECONDS)).hasCauseInstanceOf(BedrockRuntimeException.class);
        }
        assertThat(only().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
        assertThat(string(only(), SemanticConventions.OUTPUT_VALUE)).isEqualTo("partial-output");
        assertThat(only().toString()).doesNotContain("private-stream-error");
    }

    static byte[] frames(boolean tool) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(frame("messageStart", "{\"role\":\"assistant\"}"));
        if (tool) {
            out.write(frame("contentBlockStart", "{\"contentBlockIndex\":0,\"start\":{\"toolUse\":{\"toolUseId\":\"tool-id\",\"name\":\"private-tool\"}}}"));
            out.write(frame("contentBlockDelta", "{\"contentBlockIndex\":0,\"delta\":{\"toolUse\":{\"input\":\"{\\\"city\\\":\"}}}"));
            out.write(frame("contentBlockDelta", "{\"contentBlockIndex\":0,\"delta\":{\"toolUse\":{\"input\":\"\\\"Paris\\\"}\"}}}"));
        } else out.write(frame("contentBlockDelta", "{\"contentBlockIndex\":0,\"delta\":{\"text\":\"fixture-output\"}}"));
        out.write(frame("contentBlockStop", "{\"contentBlockIndex\":0}"));
        out.write(frame("messageStop", "{\"stopReason\":\"end_turn\"}"));
        out.write(frame("metadata", "{\"usage\":{\"inputTokens\":11,\"outputTokens\":7,\"totalTokens\":18,\"cacheReadInputTokens\":3,\"cacheWriteInputTokens\":2}}"));
        return out.toByteArray();
    }

    static byte[] frame(String event, String json) throws Exception {
        return frame(event, json, "event");
    }

    private static byte[] frame(String event, String json, String type) throws Exception {
        ByteArrayOutputStream headers = new ByteArrayOutputStream();
        DataOutputStream stream = new DataOutputStream(headers);
        for (String[] pair : List.of(new String[]{":message-type", type}, new String[]{type.equals("exception") ? ":exception-type" : ":event-type", event},
            new String[]{":content-type", "application/json"})) {
            byte[] key = pair[0].getBytes(StandardCharsets.UTF_8), value = pair[1].getBytes(StandardCharsets.UTF_8);
            stream.writeByte(key.length); stream.write(key); stream.writeByte(7); stream.writeShort(value.length); stream.write(value);
        }
        byte[] payload = json.getBytes(StandardCharsets.UTF_8), headerBytes = headers.toByteArray();
        int size = 16 + payload.length + headerBytes.length;
        ByteBuffer frame = ByteBuffer.allocate(size).order(ByteOrder.BIG_ENDIAN);
        frame.putInt(size).putInt(headerBytes.length);
        CRC32 crc = new CRC32(); crc.update(frame.array(), 0, 8); frame.putInt((int) crc.getValue());
        frame.put(headerBytes).put(payload); crc.reset(); crc.update(frame.array(), 0, size - 4); frame.putInt((int) crc.getValue());
        return frame.array();
    }
}
