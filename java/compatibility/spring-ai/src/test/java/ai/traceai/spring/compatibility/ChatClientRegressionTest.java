package ai.traceai.spring.compatibility;

import ai.traceai.TraceConfig;
import ai.traceai.spring.SpringAIObservationFilter;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationHandler;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.observation.AdvisorObservationContext;
import org.springframework.ai.chat.client.observation.ChatClientObservationContext;
import org.springframework.ai.chat.observation.ChatModelObservationContext;
import reactor.core.Exceptions;
import reactor.core.scheduler.Schedulers;
import static org.junit.jupiter.api.Assertions.*;

@Timeout(30)
class ChatClientRegressionTest {
    private static final String SECRET = "synthetic-private-client-provider-error";

    @ParameterizedTest @ValueSource(booleans = {false, true})
    void chatClientErrorsHonorGlobalPrivacy(boolean streaming) throws Exception {
        assertTrue(TraceConfig.fromEnvironment().isHideErrorDetails());
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            var observed = countNativeObservations(f);
            f.replies.add(new NativeSpringAITest.Reply("{\"error\":{\"message\":\"" + SECRET
                + "\",\"type\":\"invalid_request_error\"}}", "application/json", 400));
            var client = client(f);
            assertThrows(RuntimeException.class, () -> {
                if (streaming) client.prompt("synthetic input").stream().content().blockLast(Duration.ofSeconds(10));
                else client.prompt("synthetic input").call().content();
            });
            for (var span : awaitNativeSpans(f, observed)) {
                assertAll(span.getName(),
                    () -> assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode()),
                    () -> assertFalse(span.toString().contains(SECRET)),
                    () -> assertTrue(span.getStatus().getDescription().isEmpty()),
                    () -> assertEquals(1, span.getEvents().size()));
                var event = span.getEvents().get(0);
                assertEquals("exception", event.getName());
                assertEquals(1, event.getAttributes().size(), "Only the exception type may be recorded");
                var type = NativeSpringAITest.attr(span, "error.type");
                assertNotNull(type);
                assertEquals(type, event.getAttributes().get(AttributeKey.stringKey("exception.type")));
            }
        }
    }

    @ParameterizedTest @ValueSource(booleans = {false, true})
    void completedChatClientKeepsOneModelAndSuccessfulOutcome(boolean streaming) throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            var observed = countNativeObservations(f);
            if (streaming) f.replies.add(new NativeSpringAITest.Reply(NativeSpringAITest.STREAM, "text/event-stream", 200));
            else f.reply(NativeSpringAITest.CHAT);
            var client = client(f);
            if (streaming) client.prompt("synthetic input").stream().content().blockLast(Duration.ofSeconds(10));
            else assertEquals(NativeSpringAITest.OUTPUT, client.prompt("synthetic input").call().content());
            for (var span : awaitNativeSpans(f, observed)) {
                assertEquals(StatusCode.OK, span.getStatus().getStatusCode(), span.getName());
                assertTrue(span.getEvents().isEmpty());
                assertFalse(span.toString().contains(NativeSpringAITest.OUTPUT));
            }
        }
    }

    @Test void timedOutChatClientDoesNotExportSuccessfulParentOrAdvisors() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            var observed = countNativeObservations(f);
            var release = new CountDownLatch(1);
            f.server.removeContext("/");
            f.server.createContext("/", exchange -> {
                exchange.getRequestBody().readAllBytes();
                exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
                exchange.sendResponseHeaders(200, 0);
                String chunk = "data: {\"id\":\"timeout-id\",\"created\":1,\"object\":\"chat.completion.chunk\","
                    + "\"model\":\"synthetic-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\","
                    + "\"content\":\"partial\"},\"finish_reason\":null}]}\n\n";
                exchange.getResponseBody().write((chunk + chunk + chunk).getBytes(StandardCharsets.UTF_8));
                exchange.getResponseBody().flush();
                try { release.await(5, TimeUnit.SECONDS); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); }
                finally { exchange.close(); }
            });
            try {
                var client = client(f);
                var failure = assertThrows(RuntimeException.class, () -> client.prompt("synthetic input").stream().content()
                    .subscribeOn(Schedulers.boundedElastic()).timeout(Duration.ofMillis(1500))
                    .blockLast(Duration.ofSeconds(10)));
                assertInstanceOf(TimeoutException.class, Exceptions.unwrap(failure));
                assertUnknownOutcome(awaitNativeSpans(f, observed));
            } finally { release.countDown(); }
        }
    }

    @Test void cancelledChatClientDoesNotInventSuccessOrException() throws Exception {
        try (var f = new NativeSpringAITest.Fixture(new SpringAIObservationFilter())) {
            var observed = countNativeObservations(f);
            f.replies.add(new NativeSpringAITest.Reply(NativeSpringAITest.STREAM, "text/event-stream", 200));
            assertNotNull(client(f).prompt("synthetic input").stream().chatResponse()
                .take(1).blockLast(Duration.ofSeconds(10)));
            assertUnknownOutcome(awaitNativeSpans(f, observed));
        }
    }

    private static ChatClient client(NativeSpringAITest.Fixture f) {
        return ChatClient.builder(Models.chat(f.url(), f.registry), f.registry, null, null).build();
    }

    private static void assertUnknownOutcome(List<SpanData> spans) {
        for (var span : spans) {
            assertEquals(StatusCode.UNSET, span.getStatus().getStatusCode(), span.getName());
            assertTrue(span.getEvents().isEmpty(), "Cancellation must not invent an exception");
            assertNull(NativeSpringAITest.attr(span, "error.type"));
        }
    }

    private static AtomicInteger countNativeObservations(NativeSpringAITest.Fixture f) {
        var started = new AtomicInteger();
        f.registry.observationConfig().observationHandler(new ObservationHandler<Observation.Context>() {
            @Override public boolean supportsContext(Observation.Context context) {
                return context instanceof ChatModelObservationContext
                    || context instanceof ChatClientObservationContext || context instanceof AdvisorObservationContext;
            }
            @Override public void onStart(Observation.Context context) { started.incrementAndGet(); }
        });
        return started;
    }

    private static List<SpanData> nativeSpans(NativeSpringAITest.Fixture f) {
        // Native HTTP spans are intentionally excluded: they keep their own error/completion policy.
        return f.spans().stream().filter(span -> NativeSpringAITest.attr(span, "fi.span.kind") != null
            || NativeSpringAITest.attr(span, "spring.ai.kind") != null).toList();
    }

    private static List<SpanData> awaitNativeSpans(NativeSpringAITest.Fixture f, AtomicInteger observed)
            throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        while (nativeSpans(f).size() < observed.get() && System.nanoTime() < deadline) Thread.sleep(10);
        var spans = nativeSpans(f);
        assertTrue(observed.get() >= 3, "Must exercise model, client and built-in advisor observations");
        assertEquals(observed.get(), spans.size(), "Exactly one finished span per native observation");
        var model = f.onlySpan();
        assertEquals(1, spans.stream().filter(s -> "chat_client".equals(NativeSpringAITest.attr(s, "spring.ai.kind"))).count());
        assertTrue(spans.stream().anyMatch(s -> NativeSpringAITest.attr(s, "spring.ai.advisor.name") != null));
        for (var span : spans) assertEquals(model.getTraceId(), span.getTraceId(), span.getName());
        return spans;
    }
}
