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
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.core.SdkResponse;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeAsyncClient;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelResponse;

import java.lang.reflect.Proxy;
import java.net.InetSocketAddress;
import java.util.List;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.*;

/** Controlled SDK completion with the real OTLP exporter and a localhost receiver. */
class BedrockAsyncCompletionTest {
    @ParameterizedTest
    @CsvSource({
        "converse, success", "invokeModel, success",
        "converse, failure", "invokeModel, failure",
        "converse, sdk-cancel", "invokeModel, sdk-cancel",
        "converse, caller-cancel-true", "invokeModel, caller-cancel-true",
        "converse, caller-cancel-false", "invokeModel, caller-cancel-false",
        "converse, caller-failure", "invokeModel, caller-failure",
        "converse, caller-timeout", "invokeModel, caller-timeout",
        "converse, caller-success", "invokeModel, caller-success",
        "converse, caller-success-timeout", "invokeModel, caller-success-timeout"
    })
    @SuppressWarnings("unchecked")
    void endsSpanBeforeApplicationCallbackFlushesAndShutsDown(String operation, String outcome) throws Exception {
        List<ExportTraceServiceRequest> exports = new CopyOnWriteArrayList<>();
        HttpServer collector = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        collector.createContext("/v1/traces", exchange -> {
            try {
                exports.add(ExportTraceServiceRequest.parseFrom(exchange.getRequestBody().readAllBytes()));
                exchange.sendResponseHeaders(200, -1);
            } finally {
                exchange.close();
            }
        });
        collector.start();
        try (SdkTracerProvider provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(
            OtlpHttpSpanExporter.builder().setEndpoint("http://127.0.0.1:" + collector.getAddress().getPort() + "/v1/traces").build())).build()) {
            FITracer tracer = new FITracer(provider.get("async-completion-test"), TraceConfig.builder()
                .apiKey("offline").secretKey("offline").projectName("offline").hideErrorDetails(true).build());
            AtomicReference<Boolean> interrupt = new AtomicReference<>();
            CompletableFuture<SdkResponse> sdkFuture = new CompletableFuture<>() {
                @Override
                public boolean cancel(boolean mayInterruptIfRunning) {
                    interrupt.set(mayInterruptIfRunning);
                    return super.cancel(mayInterruptIfRunning);
                }
            };
            BedrockRuntimeAsyncClient sdk = (BedrockRuntimeAsyncClient) Proxy.newProxyInstance(
                BedrockRuntimeAsyncClient.class.getClassLoader(), new Class<?>[] {BedrockRuntimeAsyncClient.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals(operation)) return sdkFuture;
                    throw new UnsupportedOperationException(method.getName());
                });
            TracedBedrockRuntimeAsyncClient client = new TracedBedrockRuntimeAsyncClient(sdk, tracer);
            CompletableFuture<? extends SdkResponse> typed = operation.equals("converse")
                ? client.converse(BedrockTracingTest.request())
                : client.invokeModel(InvokeModelRequest.builder().modelId("amazon.titan-text-express-v1")
                    .body(SdkBytes.fromUtf8String("{\"inputText\":\"question\"}")).build());
            CompletableFuture<SdkResponse> future = (CompletableFuture<SdkResponse>) typed;
            boolean successful = outcome.equals("success") || outcome.startsWith("caller-success");
            SdkResponse response = operation.equals("converse") ? BedrockTracingTest.response()
                : InvokeModelResponse.builder().body(SdkBytes.fromUtf8String("{\"results\":[{\"outputText\":\"answer\"}]}")).build();
            CompletionException failure = new CompletionException(new IllegalStateException("private-error"));
            assertThat(future).isNotDone();
            CompletableFuture<Void> appCompletion = future.handle((value, error) -> {
                assertThat(provider.forceFlush().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
                assertThat(provider.shutdown().join(5, TimeUnit.SECONDS).isSuccess()).isTrue();
                // Check inside the application callback: waiting for SDK callbacks afterward masks the bug.
                assertThat(exports).hasSize(1);
                var spans = exports.get(0).getResourceSpansList().stream()
                    .flatMap(resource -> resource.getScopeSpansList().stream())
                    .flatMap(scope -> scope.getSpansList().stream()).toList();
                assertThat(spans).hasSize(1);
                assertThat(spans.get(0).getStatus().getCode()).isEqualTo(successful
                    ? Status.StatusCode.STATUS_CODE_OK : Status.StatusCode.STATUS_CODE_ERROR);
                if (successful) {
                    assertThat(value).isSameAs(response);
                    assertThat(error).isNull();
                    assertThat(sdkFuture).isCompletedWithValue(response);
                } else if (outcome.equals("failure") || outcome.equals("caller-failure")) {
                    assertThat(error).isSameAs(failure);
                    assertThat(sdkFuture).isCompletedExceptionally();
                } else if (outcome.equals("caller-timeout")) {
                    assertThat(error).isInstanceOf(TimeoutException.class);
                    assertThat(sdkFuture).isCompletedExceptionally();
                    assertThatThrownBy(sdkFuture::join).hasCause(error);
                } else {
                    assertThat(error).isInstanceOf(CancellationException.class);
                    assertThat(future.isCancelled()).isTrue();
                    assertThat(sdkFuture.isCancelled()).isTrue();
                }
                return null;
            });
            switch (outcome) {
                case "success" -> sdkFuture.complete(response);
                case "failure" -> sdkFuture.completeExceptionally(failure);
                case "caller-failure" -> assertThat(future.completeExceptionally(failure)).isTrue();
                case "caller-timeout" -> future.orTimeout(30, TimeUnit.MILLISECONDS);
                case "caller-success" -> assertThat(future.complete(response)).isTrue();
                case "caller-success-timeout" -> future.completeOnTimeout(response, 30, TimeUnit.MILLISECONDS);
                case "sdk-cancel" -> sdkFuture.cancel(false);
                case "caller-cancel-true", "caller-cancel-false" -> {
                    boolean mayInterrupt = outcome.endsWith("true");
                    assertThat(future.cancel(mayInterrupt)).isTrue();
                    assertThat(interrupt.get()).isEqualTo(mayInterrupt);
                    assertThat(sdkFuture.isCancelled()).isTrue();
                }
                default -> throw new AssertionError(outcome);
            }
            appCompletion.get(5, TimeUnit.SECONDS);
            assertThat(exports).hasSize(1);
            assertThat(sdkFuture.complete(response)).isFalse();
            assertThat(future.complete(response)).isFalse();
            assertThat(future.completeExceptionally(new IllegalStateException("late failure"))).isFalse();
            if (!outcome.contains("cancel")) {
                assertThat(future.cancel(true)).isFalse();
            } else {
                assertThat(future.cancel(false)).isTrue();
            }
            assertThat(exports).hasSize(1);
        } finally {
            collector.stop(0);
        }
    }
}
