package ai.traceai.anthropic;

import ai.traceai.FITracer;
import ai.traceai.SemanticConventions;
import com.anthropic.models.MessageCreateParams;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.junit5.OpenTelemetryExtension;
import io.opentelemetry.sdk.trace.data.SpanData;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;

import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;

class TracedAnthropicClientTest {
    @RegisterExtension
    static final OpenTelemetryExtension otel = OpenTelemetryExtension.create();

    @ParameterizedTest
    @NullSource
    @ValueSource(doubles = {0.0, 0.7})
    void shouldUnwrapPinnedSdkTemperature(Double temperature) {
        MessageCreateParams.Builder builder = MessageCreateParams.builder()
            .model("test-model").maxTokens(10).addUserMessage("hello");
        if (temperature != null) {
            builder.temperature(temperature);
        }
        assertTemperature(builder.build(), temperature);
    }

    @Test
    void shouldKeepSupportingPlainNumericTemperature() {
        assertTemperature(new CompatibleParams(0.5), 0.5);
    }

    @Test
    void shouldIgnoreNullTemperature() {
        assertTemperature(new CompatibleParams(null), null);
    }

    @Test
    void shouldIgnoreNonNumericTemperature() {
        assertTemperature(new CompatibleParams(Optional.of("unsupported")), null);
    }

    private void assertTemperature(Object params, Double expected) {
        FakeClient client = new FakeClient();
        TracedAnthropicClient traced = new TracedAnthropicClient(client,
            new FITracer(otel.getOpenTelemetry().getTracer("test")));

        Object result = traced.createMessage(params);

        assertThat(client.api.received).isSameAs(params);
        assertThat(result).isSameAs(client.api.response);
        assertThat(otel.getSpans()).hasSize(1);
        SpanData span = otel.getSpans().get(0);
        assertThat(span.getStatus().getStatusCode()).isEqualTo(StatusCode.OK);
        assertThat(span.getEvents()).isEmpty();
        assertThat(span.getAttributes().get(AttributeKey.doubleKey(
            SemanticConventions.LLM_REQUEST_TEMPERATURE))).isEqualTo(expected);
    }

    public static class CompatibleParams {
        private final Object temperature;
        CompatibleParams(Object temperature) { this.temperature = temperature; }
        public Object temperature() { return temperature; }
    }

    // Exercise the real wrapper and pinned SDK params without any provider request.
    public static class FakeClient {
        final FakeMessages api = new FakeMessages();
        public FakeMessages messages() { return api; }
    }

    public static class FakeMessages {
        Object received;
        final Object response = new Object();
        public Object create(Object params) {
            received = params;
            return response;
        }
    }
}
