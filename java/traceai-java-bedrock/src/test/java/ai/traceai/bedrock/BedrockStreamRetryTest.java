package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
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
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.retry.RetryPolicy;
import software.amazon.awssdk.core.retry.backoff.FixedDelayBackoffStrategy;
import software.amazon.awssdk.http.Protocol;
import software.amazon.awssdk.http.nio.netty.NettyNioAsyncHttpClient;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.io.ByteArrayOutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.Properties;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import static org.assertj.core.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeFalse;

/** Real retrying AWS clients with synthetic credentials and an offline event-stream server. */
class BedrockStreamRetryTest {
    private static final AttributeKey<Double> TTFT = AttributeKey.doubleKey("gen_ai.client.time_to_first_token");
    private InMemorySpanExporter exporter;
    private SdkTracerProvider provider;
    private HttpServer server;
    private ExecutorService executor;
    private final AtomicInteger requests = new AtomicInteger();
    private final CountDownLatch ready = new CountDownLatch(1);
    private final CountDownLatch release = new CountDownLatch(1);
    private volatile int firstStatus = 200;
    private volatile byte[] firstBody;
    private volatile byte[] successBody;
    private volatile boolean retry;
    private volatile boolean holdBeforeContent = true;

    @BeforeEach
    void start() throws Exception {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
        successBody = BedrockReleaseRegressionTest.frames(false);
        executor = Executors.newCachedThreadPool();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        byte[] messageStart = BedrockReleaseRegressionTest.frame("messageStart", "{\"role\":\"assistant\"}");
        byte[] emptyDelta = BedrockReleaseRegressionTest.frame("contentBlockDelta", "{\"contentBlockIndex\":0,\"delta\":{\"text\":\"\"}}");
        server.createContext("/", exchange -> {
            try {
                exchange.getRequestBody().readAllBytes();
                int attempt = requests.incrementAndGet();
                boolean first = attempt == 1 && firstBody != null;
                int status = first ? firstStatus : 200;
                byte[] body = first ? firstBody : successBody;
                exchange.getResponseHeaders().set("content-type", status == 200
                    ? "application/vnd.amazon.eventstream" : "application/json");
                if (status == 503) exchange.getResponseHeaders().set("x-amzn-errortype", "ServiceUnavailableException");
                exchange.sendResponseHeaders(status, 0);
                if (!first && holdBeforeContent) {
                    // Headers and an empty delta are not a token. Deliberately hold the first useful frame.
                    exchange.getResponseBody().write(messageStart);
                    exchange.getResponseBody().write(emptyDelta);
                    exchange.getResponseBody().flush();
                    ready.countDown();
                    release.await(10, TimeUnit.SECONDS);
                }
                exchange.getResponseBody().write(body);
                exchange.getResponseBody().flush();
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

    private FITracer tracer(boolean hide) {
        return new FITracer(provider.get("bedrock-stream-retry"), TraceConfig.builder()
            .hideInputs(hide).hideOutputs(hide).hideErrorDetails(true).build());
    }

    private BedrockRuntimeAsyncClient sdk() {
        return BedrockRuntimeAsyncClient.builder().region(Region.US_EAST_1)
            .endpointOverride(URI.create("http://127.0.0.1:" + server.getAddress().getPort()))
            .credentialsProvider(StaticCredentialsProvider.create(AwsBasicCredentials.create("synthetic-key", "synthetic-secret")))
            .httpClientBuilder(NettyNioAsyncHttpClient.builder().protocol(Protocol.HTTP1_1))
            .overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(15))
                .retryPolicy(RetryPolicy.builder().numRetries(retry ? 1 : 0)
                    .backoffStrategy(FixedDelayBackoffStrategy.create(Duration.ofMillis(80)))
                    .throttlingBackoffStrategy(FixedDelayBackoffStrategy.create(Duration.ofMillis(80))).build())
                .addExecutionInterceptor(new BedrockRuntimeInterceptor(tracer(false)))).build();
    }

    private ConverseStreamRequest request() {
        return ConverseStreamRequest.builder().modelId("amazon.nova-pro-v1:0")
            .messages(Message.builder().role(ConversationRole.USER).content(ContentBlock.fromText("synthetic-input")).build()).build();
    }

    private SpanData only() {
        assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        return exporter.getFinishedSpanItems().get(0);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void http503RetryKeepsLogicalSpanAlive(boolean hide) throws Exception {
        retry = true;
        firstStatus = 503;
        firstBody = "{\"message\":\"synthetic-retry\"}".getBytes(StandardCharsets.UTF_8);
        assertRetry(hide);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void exceptionFrameRetryDiscardsEarlierPartialOutputAndUsage(boolean hide) throws Exception {
        Properties sdk = new Properties();
        try (var resource = BedrockRuntimeAsyncClient.class.getResourceAsStream(
            "/META-INF/maven/software.amazon.awssdk/bedrockruntime/pom.properties")) {
            assertThat(resource).isNotNull();
            sdk.load(resource);
        }
        // The supported floor doesn't decode throttlingException frames as retryable exceptions.
        assumeFalse("2.25.67".equals(sdk.getProperty("version")), "Exception-frame retries need the newer SDK decoder");
        retry = true;
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        bytes.write(BedrockReleaseRegressionTest.frame("messageStart", "{\"role\":\"assistant\"}"));
        bytes.write(BedrockReleaseRegressionTest.frame("contentBlockDelta", "{\"contentBlockIndex\":0,\"delta\":{\"text\":\"discard-me\"}}"));
        bytes.write(BedrockReleaseRegressionTest.frame("metadata", "{\"usage\":{\"inputTokens\":99,\"outputTokens\":98,\"totalTokens\":197,\"cacheReadInputTokens\":97}}"));
        bytes.write(BedrockReleaseRegressionTest.frame("throttlingException", "{\"message\":\"synthetic-throttle\"}", "exception"));
        firstBody = bytes.toByteArray();
        assertRetry(hide);
    }

    private void assertRetry(boolean hide) throws Exception {
        AtomicInteger errors = new AtomicInteger();
        AtomicLong firstContent = new AtomicLong();
        var handler = ConverseStreamResponseHandler.builder().onError(error -> errors.incrementAndGet()).subscriber(event -> {
            if (event instanceof ContentBlockDeltaEvent && ((ContentBlockDeltaEvent) event).delta().text() != null
                && !((ContentBlockDeltaEvent) event).delta().text().isEmpty()) {
                firstContent.compareAndSet(0, System.nanoTime());
            }
        }).build();
        try (var client = sdk()) {
            long beforeCall = System.nanoTime();
            var future = new TracedBedrockRuntimeAsyncClient(client, tracer(hide)).converseStream(request(), handler);
            long afterCall = System.nanoTime();
            assertThat(ready.await(10, TimeUnit.SECONDS)).isTrue();
            int earlySpans = exporter.getFinishedSpanItems().size();
            long beforeContent = System.nanoTime();
            Instant releasedAt = Instant.now();
            release.countDown();
            future.get(10, TimeUnit.SECONDS);
            var data = only();
            var checks = new org.assertj.core.api.SoftAssertions();
            checks.assertThat(requests.get()).isEqualTo(2);
            checks.assertThat(errors.get()).isGreaterThanOrEqualTo(1);
            checks.assertThat(earlySpans).as("no finished span before retry content").isZero();
            checks.assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            checks.assertThat(data.getEndEpochNanos()).isGreaterThanOrEqualTo(releasedAt.getEpochSecond() * 1_000_000_000L + releasedAt.getNano());
            checks.assertThat(data.getAttributes().get(AttributeKey.longKey(SemanticConventions.LLM_TOKEN_COUNT_TOTAL))).isEqualTo(18L);
            checks.assertThat(data.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo(hide ? null : "fixture-output");
            checks.assertThat(data.toString()).doesNotContain("discard-me", "synthetic-throttle", "synthetic-retry");
            Double ttft = data.getAttributes().get(TTFT);
            checks.assertThat(ttft).as("invocation-to-first-content latency in seconds").isNotNull();
            if (ttft != null) {
                checks.assertThat(ttft).isPositive().isLessThanOrEqualTo((firstContent.get() - beforeCall) / 1e9);
                if (firstStatus == 503) checks.assertThat(ttft).isGreaterThanOrEqualTo((beforeContent - afterCall) / 1e9);
                checks.assertThat(ttft).isLessThanOrEqualTo((data.getEndEpochNanos() - data.getStartEpochNanos()) / 1e9);
            }
            checks.assertAll();
        }
    }

    @ParameterizedTest
    @CsvSource({"false,false", "false,true", "true,false", "true,true"})
    void firstContentLatencyIgnoresHeadersAndEmptyDeltasAndSurvivesHiding(boolean tool, boolean hide) throws Exception {
        successBody = BedrockReleaseRegressionTest.frames(tool);
        AtomicLong firstContent = new AtomicLong();
        var handler = ConverseStreamResponseHandler.builder().subscriber(event -> {
            if (event instanceof ContentBlockStartEvent && ((ContentBlockStartEvent) event).start().toolUse() != null
                || event instanceof ContentBlockDeltaEvent && ((ContentBlockDeltaEvent) event).delta().text() != null
                    && !((ContentBlockDeltaEvent) event).delta().text().isEmpty()) {
                firstContent.compareAndSet(0, System.nanoTime());
            }
        }).build();
        try (var client = sdk()) {
            long beforeCall = System.nanoTime();
            var future = new TracedBedrockRuntimeAsyncClient(client, tracer(hide)).converseStream(request(), handler);
            long afterCall = System.nanoTime();
            assertThat(ready.await(10, TimeUnit.SECONDS)).isTrue();
            // A measurable delay after headers catches recording TTFT on response/messageStart/empty text.
            assertThat(future.isDone()).isFalse();
            assertThat(release.await(80, TimeUnit.MILLISECONDS)).isFalse();
            long beforeContent = System.nanoTime();
            release.countDown();
            future.get(10, TimeUnit.SECONDS);
            var data = only();
            assertThat(data.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(data.getAttributes().get(TTFT)).isNotNull()
                .isGreaterThanOrEqualTo((beforeContent - afterCall) / 1e9)
                .isLessThanOrEqualTo((firstContent.get() - beforeCall) / 1e9);
            assertThat(data.getAttributes().get(TTFT)).isLessThanOrEqualTo((data.getEndEpochNanos() - data.getStartEpochNanos()) / 1e9);
            if (hide) assertThat(data.getAttributes().toString()).doesNotContain("fixture-output", "private-tool", "Paris");
        }
    }

    @Test
    void errorBeforeContentDoesNotInventFirstTokenLatency() throws Exception {
        firstStatus = 503;
        firstBody = "{\"message\":\"synthetic-terminal-error\"}".getBytes(StandardCharsets.UTF_8);
        try (var client = sdk()) {
            var handler = ConverseStreamResponseHandler.builder().subscriber(event -> { }).build();
            var future = new TracedBedrockRuntimeAsyncClient(client, tracer(false)).converseStream(request(), handler);
            assertThatThrownBy(() -> future.get(10, TimeUnit.SECONDS)).hasCauseInstanceOf(BedrockRuntimeException.class);
            assertThat(only().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(only().getAttributes().get(TTFT)).isNull();
        }
    }

    @Test
    void cancellationBeforeContentDoesNotInventFirstTokenLatency() throws Exception {
        try (var client = sdk()) {
            var handler = ConverseStreamResponseHandler.builder().subscriber(event -> { }).build();
            var future = new TracedBedrockRuntimeAsyncClient(client, tracer(false)).converseStream(request(), handler);
            assertThat(ready.await(10, TimeUnit.SECONDS)).isTrue();
            assertThat(future.cancel(true)).isTrue();
            assertThat(only().getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(only().getAttributes().get(TTFT)).isNull();
        }
    }
}
