package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.core.client.config.ClientOverrideConfiguration;
import software.amazon.awssdk.core.interceptor.ExecutionAttributes;
import software.amazon.awssdk.core.interceptor.InterceptorContext;
import software.amazon.awssdk.core.retry.RetryPolicy;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.http.Protocol;
import software.amazon.awssdk.http.nio.netty.NettyNioAsyncHttpClient;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.*;

class BedrockRuntimeInterceptorTest {
    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final CompletableFuture<SpanData> ended = new CompletableFuture<>();
    private final CompletableFuture<Void> secondEnded = new CompletableFuture<>();
    private final CountDownLatch received = new CountDownLatch(1);
    private final CountDownLatch release = new CountDownLatch(1);
    private final AtomicInteger attempts = new AtomicInteger();
    private SdkTracerProvider provider;
    private HttpServer server;
    private ExecutorService executor;
    private FITracer tracer;
    private volatile boolean block;
    private volatile int failures;
    private volatile int failureCode = 500;
    private volatile String response = "{\"output\":{\"message\":{\"role\":\"assistant\",\"content\":[{\"text\":\"answer\"}]}},"
        + "\"stopReason\":\"end_turn\",\"usage\":{\"inputTokens\":3,\"outputTokens\":5,\"totalTokens\":8},\"metrics\":{\"latencyMs\":1}}";

    @BeforeEach
    void setup() throws Exception {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter))
            .addSpanProcessor(new SpanProcessor() {
                public void onStart(Context parent, ReadWriteSpan span) { }
                public boolean isStartRequired() { return false; }
                public void onEnd(ReadableSpan span) {
                    ended.complete(span.toSpanData());
                    if (exporter.getFinishedSpanItems().size() >= 2) secondEnded.complete(null);
                }
                public boolean isEndRequired() { return true; }
            }).build();
        tracer = new FITracer(provider.get("bedrock-test"), TraceConfig.builder()
            .apiKey("offline").secretKey("offline").projectName("offline").hideErrorDetails(true).build());
        executor = Executors.newCachedThreadPool();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        server.createContext("/", exchange -> {
            try {
                exchange.getRequestBody().readAllBytes();
                int attempt = attempts.incrementAndGet();
                received.countDown();
                if (block && !release.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("test response timed out");
                boolean fail = attempt <= failures;
                byte[] body = (fail ? "{\"message\":\"private-error\"}" : response).getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.getResponseHeaders().set("x-amzn-requestid", "offline-request");
                if (fail) exchange.getResponseHeaders().set("x-amzn-errortype", failureCode == 400 ? "ValidationException" : "InternalServerException");
                exchange.sendResponseHeaders(fail ? failureCode : 200, body.length);
                exchange.getResponseBody().write(body);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
            } finally {
                exchange.close();
            }
        });
        server.start();
    }

    @AfterEach
    void teardown() {
        release.countDown();
        server.stop(0);
        executor.shutdownNow();
        provider.close();
    }

    @Test
    void syncHookPreservesParentAndDoesNotLeakScope() {
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        try (BedrockRuntimeClient client = sync(); Scope ignored = parent.makeCurrent()) {
            assertThat(client.converse(BedrockTracingTest.request()).output().message().content().get(0).text()).isEqualTo("answer");
            assertThat(Span.current()).isSameAs(parent);
            SpanData span = exporter.getFinishedSpanItems().get(0);
            assertThat(span.getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        } finally {
            parent.end();
        }
    }

    @Test
    void asyncFacadeCapturesTheCallingContext() throws Exception {
        Span parent = provider.get("test").spanBuilder("parent").startSpan();
        try (BedrockRuntimeAsyncClient client = async()) {
            CompletableFuture<ConverseResponse> future;
            try (Scope ignored = parent.makeCurrent()) {
                future = new TracedBedrockRuntimeAsyncClient(client, tracer).converse(BedrockTracingTest.request());
                assertThat(Span.current()).isSameAs(parent);
            }
            assertThat(future.get(5, TimeUnit.SECONDS).usage().totalTokens()).isEqualTo(8);
            assertThat(ended.get(5, TimeUnit.SECONDS).getParentSpanId()).isEqualTo(parent.getSpanContext().getSpanId());
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        } finally {
            parent.end();
        }
    }

    @Test
    void invokeModelWorksOnBothSdkClients() throws Exception {
        response = "{\"results\":[{\"outputText\":\"answer\"}]}";
        InvokeModelRequest request = InvokeModelRequest.builder().modelId("global.amazon.titan-text-express-v1")
            .contentType("application/json").body(SdkBytes.fromUtf8String("{\"inputText\":\"question\"}")).build();
        try (BedrockRuntimeClient client = sync(); BedrockRuntimeAsyncClient async = async()) {
            assertThat(client.invokeModel(request).body().asUtf8String()).isEqualTo(response);
            assertThat(new TracedBedrockRuntimeAsyncClient(async, tracer).invokeModel(request).get(5, TimeUnit.SECONDS).body().asUtf8String()).isEqualTo(response);
            secondEnded.get(5, TimeUnit.SECONDS);
            assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span ->
                assertThat(span.getAttributes().get(AttributeKey.stringKey(SemanticConventions.OUTPUT_VALUE))).isEqualTo("answer"));
        }
    }

    @Test
    void facadeAndRepeatedHookRegistrationDoNotDuplicateSpans() {
        try (BedrockRuntimeClient client = sync()) {
            var wrapper = new TracedBedrockRuntimeClient(client, tracer);
            assertThat(wrapper.unwrap()).isSameAs(client);
            wrapper.converse(BedrockTracingTest.request());
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        }
    }

    @Test
    void retriesHaveOneLogicalSpan() {
        failures = 1;
        try (BedrockRuntimeClient client = sync()) {
            client.converse(BedrockTracingTest.request());
            assertThat(attempts.get()).isEqualTo(2);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
            assertThat(exporter.getFinishedSpanItems().get(0).getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        }
    }

    @Test
    void failedCallsEndOnceAndRespectErrorPrivacy() throws Exception {
        failures = Integer.MAX_VALUE;
        failureCode = 400;
        try (BedrockRuntimeAsyncClient client = async()) {
            assertThatThrownBy(() -> new TracedBedrockRuntimeAsyncClient(client, tracer).converse(BedrockTracingTest.request()).get(5, TimeUnit.SECONDS))
                .hasCauseInstanceOf(ValidationException.class);
            SpanData span = ended.get(5, TimeUnit.SECONDS);
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getStatus().getDescription()).isEmpty();
            assertThat(span.getAttributes().toString() + span.getEvents()).doesNotContain("private-error");
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        }
    }

    @Test
    void cancellingAsyncCallsEndsTheSpan() throws Exception {
        block = true;
        try (BedrockRuntimeAsyncClient client = async()) {
            CompletableFuture<ConverseResponse> future = new TracedBedrockRuntimeAsyncClient(client, tracer).converse(BedrockTracingTest.request());
            assertThat(received.await(5, TimeUnit.SECONDS)).isTrue();
            assertThat(future.cancel(true)).isTrue();
            assertThat(ended.get(5, TimeUnit.SECONDS).getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(exporter.getFinishedSpanItems()).hasSize(1);
        } finally {
            release.countDown();
        }
    }

    @Test
    void streamingRequestsAreNotMisrepresentedAsCompletedCalls() {
        BedrockRuntimeInterceptor interceptor = new BedrockRuntimeInterceptor(tracer);
        ExecutionAttributes attributes = new ExecutionAttributes();
        InterceptorContext context = InterceptorContext.builder().request(ConverseStreamRequest.builder().modelId("model").build())
            .response(ConverseStreamResponse.builder().build()).build();
        interceptor.beforeExecution(context, attributes);
        interceptor.afterExecution(context, attributes);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
    }

    private ClientOverrideConfiguration configuration() {
        return ClientOverrideConfiguration.builder().apiCallTimeout(Duration.ofSeconds(8))
            .retryPolicy(RetryPolicy.builder().numRetries(1).build())
            .addExecutionInterceptor(new BedrockRuntimeInterceptor(tracer))
            .addExecutionInterceptor(new BedrockRuntimeInterceptor(tracer)).build();
    }

    private BedrockRuntimeClient sync() {
        return BedrockRuntimeClient.builder().endpointOverride(endpoint()).region(Region.US_EAST_1)
            .credentialsProvider(StaticCredentialsProvider.create(AwsBasicCredentials.create("offline", "offline")))
            .overrideConfiguration(configuration()).build();
    }

    private BedrockRuntimeAsyncClient async() {
        // The local JDK fixture speaks HTTP/1.1; current Bedrock SDKs default async clients to HTTP/2.
        return BedrockRuntimeAsyncClient.builder()
            .httpClientBuilder(NettyNioAsyncHttpClient.builder().protocol(Protocol.HTTP1_1))
            .endpointOverride(endpoint()).region(Region.US_EAST_1)
            .credentialsProvider(StaticCredentialsProvider.create(AwsBasicCredentials.create("offline", "offline")))
            .overrideConfiguration(configuration()).build();
    }

    private URI endpoint() {
        return URI.create("http://127.0.0.1:" + server.getAddress().getPort());
    }
}
