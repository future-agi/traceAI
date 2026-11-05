package ai.traceai.spring.compatibility;

import ai.traceai.FITracer;
import ai.traceai.TraceConfig;
import ai.traceai.spring.SpringAIObservationFilter;
import ai.traceai.spring.TracedChatModel;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationHandler;
import io.micrometer.observation.ObservationRegistry;
import io.opentelemetry.api.trace.StatusCode;
import java.time.Duration;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.ai.chat.observation.ChatModelObservationContext;
import org.springframework.ai.chat.prompt.Prompt;
import static org.junit.jupiter.api.Assertions.*;

@Timeout(30)
class RecallRegressionTest {
    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void nativeErrorsHonorGlobalErrorPrivacy(boolean streaming) throws Exception {
        assertTrue(TraceConfig.fromEnvironment().isHideErrorDetails(), "test launch must enable FI_HIDE_ERROR_DETAILS");
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.replies.add(new NativeSpringAITest.Reply("{\"error\":{\"message\":\"synthetic-sensitive-marker-29a\",\"type\":\"invalid_request_error\"}}", "application/json", 400));
            var observed = new java.util.concurrent.atomic.AtomicReference<Throwable>();
            f.registry.observationConfig().observationHandler(new ObservationHandler<Observation.Context>() {
                @Override public boolean supportsContext(Observation.Context context) {
                    return context instanceof ChatModelObservationContext;
                }
                @Override public void onError(Observation.Context context) {
                    observed.set(context.getError());
                }
            });
            var model = Models.chat(f.url(), f.registry);
            var error = assertThrows(RuntimeException.class, () -> {
                if (streaming) model.stream(new Prompt("synthetic input")).blockLast(Duration.ofSeconds(10));
                else model.call(new Prompt("synthetic input"));
            });
            assertSame(observed.get(), error, "The application's original exception is unchanged");
            if (!streaming) assertTrue(error.getMessage().contains("synthetic-sensitive-marker-29a"));
            var span = f.onlySpan();
            assertEquals(error.getClass().getName(), NativeSpringAITest.attr(span, "error.type"));
            assertEquals(1, span.getEvents().size());
            assertEquals(error.getClass().getName(), span.getEvents().get(0).getAttributes()
                .get(io.opentelemetry.api.common.AttributeKey.stringKey("exception.type")));
            assertEquals(1, span.getEvents().get(0).getAttributes().size());
            assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode());
            String emittedError = span.getStatus().getDescription() + span.getEvents().toString() + span.getAttributes().toString();
            System.out.println("RECALL native error privacy: status contains marker=" + span.getStatus().getDescription().contains("synthetic-sensitive-marker-29a")
                + "; events contain marker=" + span.getEvents().toString().contains("synthetic-sensitive-marker-29a")
                + "; combined contains marker=" + emittedError.contains("synthetic-sensitive-marker-29a"));
            assertFalse(emittedError.contains("synthetic-sensitive-marker-29a"), "FI_HIDE_ERROR_DETAILS=true must hide the provider response body from exported spans");
        }
    }

    @Test void legacyErrorPrivacyControl() throws Exception {
        assertTrue(TraceConfig.fromEnvironment().isHideErrorDetails());
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.replies.add(new NativeSpringAITest.Reply("{\"error\":{\"message\":\"synthetic-sensitive-marker-29a\",\"type\":\"invalid_request_error\"}}", "application/json", 400));
            var model = new TracedChatModel(Models.chat(f.url(), ObservationRegistry.NOOP), new FITracer(f.sdk.get("recall")), "openai");
            assertThrows(RuntimeException.class, () -> model.call(new Prompt("synthetic input")));
            var span = f.onlySpan();
            assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode());
            assertFalse((span.getStatus().getDescription() + span.getEvents() + span.getAttributes()).contains("synthetic-sensitive-marker-29a"));
        }
    }

    @Test void wrapperSyncRecordsDefaultRequestModelNotResponseModel() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.reply(NativeSpringAITest.CHAT.replace("synthetic-model", "synthetic-response-snapshot"));
            var model = new TracedChatModel(Models.chat(f.url(), ObservationRegistry.NOOP), new FITracer(f.sdk.get("recall")), "openai");
            assertEquals("synthetic-model", model.getDefaultOptions().getModel());
            model.call(new Prompt("synthetic input"));
            var span = f.onlySpan();
            System.out.println("RECALL sync request=" + NativeSpringAITest.attr(span, "gen_ai.request.model") + "; response=" + NativeSpringAITest.attr(span, "gen_ai.response.model"));
            assertEquals("synthetic-model", NativeSpringAITest.attr(span, "gen_ai.request.model"));
            assertEquals("synthetic-response-snapshot", NativeSpringAITest.attr(span, "gen_ai.response.model"));
        }
    }

    @Test void wrapperStreamRecordsDefaultRequestModel() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            f.replies.add(new NativeSpringAITest.Reply(NativeSpringAITest.STREAM.replace("synthetic-model", "synthetic-response-snapshot"), "text/event-stream", 200));
            var model = new TracedChatModel(Models.chat(f.url(), ObservationRegistry.NOOP), new FITracer(f.sdk.get("recall")), "openai");
            assertEquals("synthetic-model", model.getDefaultOptions().getModel());
            model.stream(new Prompt("synthetic input")).blockLast(Duration.ofSeconds(10));
            var span = f.onlySpan();
            System.out.println("RECALL stream request=" + NativeSpringAITest.attr(span, "gen_ai.request.model") + "; response=" + NativeSpringAITest.attr(span, "gen_ai.response.model"));
            assertEquals("synthetic-model", NativeSpringAITest.attr(span, "gen_ai.request.model"));
            assertEquals("synthetic-response-snapshot", NativeSpringAITest.attr(span, "gen_ai.response.model"));
        }
    }

    @Test void nativeTimedOutOperationIsNotExportedAsSuccessful() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            var release = new java.util.concurrent.CountDownLatch(1);
            f.server.removeContext("/");
            f.server.createContext("/", exchange -> {
                exchange.getRequestBody().readAllBytes();
                exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
                exchange.sendResponseHeaders(200, 0);
                String chunk = "data: {\"id\":\"timeout-id\",\"created\":1,\"object\":\"chat.completion.chunk\",\"model\":\"synthetic-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"partial\"},\"finish_reason\":null}]}\n\n";
                exchange.getResponseBody().write((chunk + chunk + chunk).getBytes(java.nio.charset.StandardCharsets.UTF_8));
                exchange.getResponseBody().flush();
                try { release.await(3, java.util.concurrent.TimeUnit.SECONDS); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); }
                finally { exchange.close(); }
            });
            try {
                RuntimeException failure = assertThrows(RuntimeException.class, () -> Models.chat(f.url(), f.registry)
                    .stream(new Prompt("timeout input")).subscribeOn(reactor.core.scheduler.Schedulers.boundedElastic())
                    .timeout(Duration.ofMillis(750)).blockLast(Duration.ofSeconds(10)));
                assertInstanceOf(java.util.concurrent.TimeoutException.class, reactor.core.Exceptions.unwrap(failure));
                var span = f.onlySpan();
                System.out.println("RECALL native timeout status=" + span.getStatus().getStatusCode());
                assertNotEquals(StatusCode.OK, span.getStatus().getStatusCode(), "A timed-out operation must not be exported as successful");
            } finally { release.countDown(); }
        }
    }

    @Test void nativeCancellationDoesNotClaimSuccess() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter()); var stalled = f.stallStream()) {
            assertNotNull(Models.chat(f.url(), f.registry).stream(new Prompt("synthetic input"))
                .take(1).blockLast(Duration.ofSeconds(10)));
            var span = f.onlySpan();
            assertEquals(StatusCode.UNSET, span.getStatus().getStatusCode());
            assertTrue(span.getEvents().isEmpty(), "Cancellation does not invent a provider error");
        }
    }
}
