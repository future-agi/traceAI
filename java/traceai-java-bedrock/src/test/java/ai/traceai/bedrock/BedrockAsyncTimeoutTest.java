package ai.traceai.bedrock;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.http.Protocol;
import software.amazon.awssdk.http.nio.netty.NettyNioAsyncHttpClient;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import java.lang.reflect.Proxy;
import java.net.InetSocketAddress;
import java.net.URI;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;
import static org.assertj.core.api.Assertions.*;

class BedrockAsyncTimeoutTest {
    @ParameterizedTest
    @CsvSource({"converse,false", "converse,true", "invokeModel,false", "invokeModel,true"})
    void timeoutsPreserveTheSdkCompletionContract(String operation, boolean wrapped) throws Exception {
        CountDownLatch received = new CountDownLatch(1), release = new CountDownLatch(1);
        var exporter = InMemorySpanExporter.create();
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
        AtomicReference<CompletableFuture<?>> original = new AtomicReference<>();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
             var sdk = BedrockRuntimeAsyncClient.builder().region(Region.US_EAST_1)
                 .endpointOverride(URI.create("http://127.0.0.1:" + server.getAddress().getPort()))
                 .credentialsProvider(StaticCredentialsProvider.create(AwsBasicCredentials.create("synthetic", "synthetic")))
                 .httpClientBuilder(NettyNioAsyncHttpClient.builder().protocol(Protocol.HTTP1_1))
                 .build()) {
            var observedSdk = (BedrockRuntimeAsyncClient) Proxy.newProxyInstance(BedrockRuntimeAsyncClient.class.getClassLoader(),
                new Class<?>[]{BedrockRuntimeAsyncClient.class}, (proxy, method, args) -> {
                    Object value = method.invoke(sdk, args);
                    if (value instanceof CompletableFuture) original.set((CompletableFuture<?>)value);
                    return value;
                });
            FITracer tracer = new FITracer(provider.get("async-timeout-test"), TraceConfig.builder()
                .apiKey("synthetic").secretKey("synthetic").projectName("offline").hideErrorDetails(true).build());
            var facade = new TracedBedrockRuntimeAsyncClient(observedSdk, tracer);
            var invoke = InvokeModelRequest.builder().modelId("amazon.titan-text-express-v1")
                .contentType("application/json").body(SdkBytes.fromUtf8String("{\"inputText\":\"hello\"}")).build();
            CompletableFuture<?> result = operation.equals("converse")
                ? (wrapped ? facade.converse(BedrockTracingTest.request()) : observedSdk.converse(BedrockTracingTest.request()))
                : (wrapped ? facade.invokeModel(invoke) : observedSdk.invokeModel(invoke));
            assertThat(received.await(5, TimeUnit.SECONDS)).isTrue();
            CompletableFuture<Void> observed = result.handle((value, error) -> {
                assertThat(error).isInstanceOf(TimeoutException.class);
                org.assertj.core.api.SoftAssertions.assertSoftly(soft -> {
                    soft.assertThat(original.get().isCompletedExceptionally()).as("SDK timeout completion").isTrue();
                    if (wrapped) soft.assertThat(exporter.getFinishedSpanItems()).as("trace available before application shutdown").hasSize(1);
                });
                return null;
            });
            result.orTimeout(30, TimeUnit.MILLISECONDS);
            observed.get(5, TimeUnit.SECONDS);
        } finally {
            if (original.get() != null) original.get().cancel(true);
            release.countDown();
            server.stop(0);
            executor.shutdownNow();
        }
    }
}
