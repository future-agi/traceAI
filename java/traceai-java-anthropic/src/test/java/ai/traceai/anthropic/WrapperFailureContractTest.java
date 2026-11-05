package ai.traceai.anthropic;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import ai.traceai.TraceConfig;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;

import static org.junit.jupiter.api.Assertions.*;

/** Reflection must not change caller-visible failures or leave the span scope open. */
class WrapperFailureContractTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @Test
    void runtimeExceptionIdentityAndContextArePreserved() {
        assertFailure(new IllegalStateException("synthetic-private-failure"));
    }

    @Test
    void errorIdentityAndContextArePreserved() {
        assertFailure(new AssertionError("synthetic-private-failure"));
    }

    private void assertFailure(Throwable original) {
        var tracer = otel.getOpenTelemetry().getTracer("wrapper-failure-contract");
        var config = TraceConfig.builder().hideErrorDetails(true).build();
        var client = new FailingClient(original);
        var wrapper = new TracedAnthropicClient(client, new FITracer(tracer, config));
        var parent = tracer.spanBuilder("parent").startSpan();
        try (var scope = parent.makeCurrent()) {
            Throwable actual = assertThrows(original.getClass(), () -> wrapper.createMessage(new Object()));
            assertSame(original, actual, "rethrow the original failure, not a replacement of the same type");
            assertEquals(parent.getSpanContext(), Span.current().getSpanContext());
            assertEquals(1, otel.getSpans().size());
            var span = otel.getSpans().get(0);
            assertTrue(span.hasEnded());
            assertEquals(parent.getSpanContext().getSpanId(), span.getParentSpanId());
            assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode());
            assertEquals(original.getClass().getName(), span.getAttributes().get(
                AttributeKey.stringKey(SemanticConventions.ERROR_TYPE)));
            assertEquals(1, span.getEvents().size());
            assertEquals(original.getClass().getName(), span.getEvents().get(0).getAttributes().get(
                AttributeKey.stringKey("exception.type")));
            assertFalse(span.toString().contains("synthetic-private-failure"));
        } finally {
            parent.end();
        }
        assertEquals(2, otel.getSpans().size());
    }

    public static final class FailingClient {
        private final FailingMessages messages;
        FailingClient(Throwable failure) { messages = new FailingMessages(failure); }
        public FailingMessages messages() { return messages; }
    }

    public static final class FailingMessages {
        private final Throwable failure;
        FailingMessages(Throwable failure) { this.failure = failure; }
        public Object create(Object params) {
            if (failure instanceof RuntimeException) throw (RuntimeException) failure;
            throw (Error) failure;
        }
    }
}
