package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.trace.v1.Status;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.core.SdkResponse;
import software.amazon.awssdk.http.Protocol;
import software.amazon.awssdk.http.nio.netty.NettyNioAsyncHttpClient;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelResponse;

import java.lang.reflect.Proxy;
import java.net.InetSocketAddress;
import java.net.URI;
import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.*;

/** Native SDK controls and wrapped calls using real localhost SDK and OTLP HTTP transports. */
class BedrockAsyncTimeoutTest {
    @ParameterizedTest
    @CsvSource({
        "converse,false,timeout", "converse,true,timeout",
        "invokeModel,false,timeout", "invokeModel,true,timeout",
        "converse,false,success-timeout", "converse,true,success-timeout",
        "invokeModel,false,success-timeout", "invokeModel,true,success-timeout",
        "converse,false,success", "converse,true,success",
        "invokeModel,false,success", "invokeModel,true,success"
    })
    @SuppressWarnings("unchecked")
    void callerCompletionPreservesTheSdkCompletionContract(String operation, boolean wrapped, String outcome) throws Exception {
        CountDownLatch received = new CountDownLatch(1), release = new CountDownLatch(1);
        var executor = Executors.newCachedThreadPool();
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        server.createContext("/", exchange -> {
            try {
                exchange.getRequestBody().readAllBytes();
                received.countDown();
                release.await(10, TimeUnit.SECONDS);
                byte[] body = "{}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
                exchange.sendResponseHeaders(200, body.length);
                exchange.getResponseBody().write(body);
            } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            finally { exchange.close(); }
        });
        server.start();
        List<ExportTraceServiceRequest> exports = new CopyOnWriteArrayList<>();
        var collector = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        collector.createContext("/v1/traces", exchange -> {
            try {
                exports.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody().readAllBytes()));
                exchange.sendResponseHeaders(200, -1);
            } finally { exchange.close(); }
        });
        collector.start();
        AtomicReference<CompletableFuture<SdkResponse>> original = new AtomicReference<>();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(
                OtlpHttpSpanExporter.builder().setEndpoint("http://127.0.0.1:" + collector.getAddress().getPort() + "/v1/traces").build())).build();
             var sdk = BedrockRuntimeAsyncClient.builder().region(Region.US_EAST_1)
                 .endpointOverride(URI.create("http://127.0.0.1:" + server.getAddress().getPort()))
                 .credentialsProvider(StaticCredentialsProvider.create(AwsBasicCredentials.create("synthetic", "synthetic")))
                 .httpClientBuilder(NettyNioAsyncHttpClient.builder().protocol(Protocol.HTTP1_1))
                 .build()) {
            var observedSdk = (BedrockRuntimeAsyncClient) Proxy.newProxyInstance(BedrockRuntimeAsyncClient.class.getClassLoader(),
                new Class<?>[]{BedrockRuntimeAsyncClient.class}, (proxy, method, args) -> {
                    Object value = method.invoke(sdk, args);
                    if (value instanceof CompletableFuture) original.set((CompletableFuture<SdkResponse>)value);
                    return value;
                });
            FITracer tracer = new FITracer(provider.get("async-timeout-test"), TraceConfig.builder()
                .apiKey("synthetic").secretKey("synthetic").projectName("offline").hideErrorDetails(true).build());
            var facade = new TracedBedrockRuntimeAsyncClient(observedSdk, tracer);
            var invoke = InvokeModelRequest.builder().modelId("amazon.titan-text-express-v1")
                .contentType("application/json").body(SdkBytes.fromUtf8String("{\"inputText\":\"hello\"}")).build();
            CompletableFuture<? extends SdkResponse> typed = operation.equals("converse")
                ? (wrapped ? facade.converse(BedrockTracingTest.request()) : observedSdk.converse(BedrockTracingTest.request()))
                : (wrapped ? facade.invokeModel(invoke) : observedSdk.invokeModel(invoke));
            CompletableFuture<SdkResponse> result = (CompletableFuture<SdkResponse>) typed;
            SdkResponse fallback = operation.equals("converse") ? BedrockTracingTest.response()
                : InvokeModelResponse.builder().body(SdkBytes.fromUtf8String("{\"results\":[{\"outputText\":\"fallback\"}]}")).build();
            assertThat(received.await(5, TimeUnit.SECONDS)).isTrue();
            CompletableFuture<Void> observed = result.handle((value, error) -> {
                assertThat(provider.forceFlush().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
                assertThat(provider.shutdown().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
                if (outcome.equals("timeout")) {
                    assertThat(error).isInstanceOf(TimeoutException.class);
                    assertThat(original.get()).as("SDK timeout completion").isCompletedExceptionally();
                    assertThatThrownBy(original.get()::join).hasCause(error);
                } else {
                    assertThat(value).isSameAs(fallback);
                    assertThat(error).isNull();
                    assertThat(original.get()).as("SDK successful completion before application callback")
                        .isCompletedWithValue(fallback);
                    assertThat(original.get().join()).isSameAs(fallback);
                }
                if (wrapped) {
                    assertThat(exports).as("span exported before application shutdown").hasSize(1);
                    var spans = exports.get(0).getResourceSpansList().stream()
                        .flatMap(resource -> resource.getScopeSpansList().stream())
                        .flatMap(scope -> scope.getSpansList().stream()).toList();
                    assertThat(spans).hasSize(1);
                    assertThat(spans.get(0).getStatus().getCode()).isEqualTo(outcome.equals("timeout")
                        ? Status.StatusCode.STATUS_CODE_ERROR : Status.StatusCode.STATUS_CODE_OK);
                } else {
                    assertThat(exports).isEmpty();
                }
                return null;
            });
            switch (outcome) {
                case "timeout" -> result.orTimeout(30, TimeUnit.MILLISECONDS);
                case "success-timeout" -> result.completeOnTimeout(fallback, 30, TimeUnit.MILLISECONDS);
                case "success" -> assertThat(result.complete(fallback)).isTrue();
                default -> throw new AssertionError(outcome);
            }
            observed.get(5, TimeUnit.SECONDS);
            assertThat(original.get().complete(fallback)).isFalse();
            assertThat(result.complete(fallback)).isFalse();
            assertThat(result.completeExceptionally(new IllegalStateException("late failure"))).isFalse();
            assertThat(result.cancel(true)).isFalse();
            assertThat(exports).hasSize(wrapped ? 1 : 0);
        } finally {
            if (original.get() != null) original.get().cancel(true);
            release.countDown();
            server.stop(0);
            collector.stop(0);
            executor.shutdownNow();
        }
    }
}
