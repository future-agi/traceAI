package ai.traceai.langchain4j;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Flow;

import static org.assertj.core.api.Assertions.*;

class TracedAiServicesTest {
    interface AsyncService { CompletableFuture<String> chat(String input); }
    interface SyncService { String chat(String input); }
    interface ReactiveService { Flow.Publisher<String> chat(String input); }

    private final InMemorySpanExporter exporter = InMemorySpanExporter.create();
    private final SdkTracerProvider provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter)).build();
    private final FITracer tracer = new FITracer(provider.get("test"), TraceConfig.builder()
            .hideInputs(true).hideOutputs(true).hideErrorDetails(true).build());
    @AfterEach void close() { provider.close(); }

    @Test void waitsForCompletionOnAnotherThreadAndPreservesFutureIdentity() throws Exception {
        CompletableFuture<String> result = new CompletableFuture<>();
        AsyncService service = TracedAiServices.create(AsyncService.class, input -> result, tracer);
        assertThat(service.chat("private-input")).isSameAs(result);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
        Thread worker = new Thread(() -> result.complete("private-output"));
        worker.start();
        worker.join();
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
            assertThat(span.getAttributes().toString()).doesNotContain("private-");
        });
    }

    @Test void asynchronousFailureAndCancellationAreNotReportedAsSuccess() {
        for (boolean cancel : new boolean[]{false, true}) {
            CompletableFuture<String> result = new CompletableFuture<>();
            AsyncService service = TracedAiServices.create(AsyncService.class, input -> result, tracer);
            service.chat("private-input");
            if (cancel) result.cancel(false);
            else result.completeExceptionally(new IllegalStateException("private-error"));
        }
        assertThat(exporter.getFinishedSpanItems()).hasSize(2).allSatisfy(span -> {
            assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.ERROR);
            assertThat(span.getAttributes().toString()).doesNotContain("private-");
        });
    }

    @Test void hiddenParametersDoNotLeakThroughCustomAttributes() {
        SyncService service = TracedAiServices.create(SyncService.class, input -> "private-output", tracer);
        assertThat(service.chat("private-input")).isEqualTo("private-output");
        assertThat(exporter.getFinishedSpanItems()).singleElement().satisfies(span ->
                assertThat(span.getAttributes().toString()).doesNotContain("private-"));
    }

    @Test void doesNotPretendLazyPublisherConstructionIsACompletedServiceInvocation() {
        Flow.Publisher<String> publisher = subscriber -> { throw new AssertionError("Must not subscribe"); };
        ReactiveService service = TracedAiServices.create(ReactiveService.class, input -> publisher, tracer);
        assertThat(service.chat("private-input")).isSameAs(publisher);
        assertThat(exporter.getFinishedSpanItems()).isEmpty();
    }
}
