import com.anthropic.client.AnthropicClient;
import com.anthropic.client.okhttp.AnthropicOkHttpClient;
import com.anthropic.core.opentelemetry.OpenTelemetryContentMode;
import com.anthropic.core.opentelemetry.OpenTelemetryOptions;
import com.anthropic.core.opentelemetry.OpenTelemetryTraces;
import com.anthropic.models.messages.MessageCreateParams;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.propagation.W3CTraceContextPropagator;
import io.opentelemetry.context.propagation.ContextPropagators;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;

public final class AnthropicTracingExample {
    public static void main(String[] args) {
        String project = System.getenv("FI_PROJECT_NAME");
        var exporter = OtlpHttpSpanExporter.builder()
            .setEndpoint(System.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
            .addHeader("X-Api-Key", System.getenv("FI_API_KEY"))
            .addHeader("X-Secret-Key", System.getenv("FI_SECRET_KEY"))
            .addHeader("X-Project-Name", project)
            .build();

        try (var provider = SdkTracerProvider.builder()
            .setResource(Resource.getDefault().merge(Resource.create(Attributes.of(
                AttributeKey.stringKey("service.name"), "anthropic-java-app",
                AttributeKey.stringKey("project_name"), project))))
            .addSpanProcessor(BatchSpanProcessor.builder(exporter).build())
            .build()) {
            OpenTelemetrySdk.builder().setTracerProvider(provider)
                .setPropagators(ContextPropagators.create(W3CTraceContextPropagator.getInstance()))
                .buildAndRegisterGlobal();

            AnthropicClient client = AnthropicOkHttpClient.builder().fromEnv()
                .openTelemetry(OpenTelemetryOptions.builder()
                    .traces(OpenTelemetryTraces.builder()
                        .contentMode(OpenTelemetryContentMode.METADATA_ONLY).build())
                    .build())
                .build();
            try {
                var message = client.messages().create(MessageCreateParams.builder()
                    .model("claude-opus-5-5")
                    .maxTokens(1024)
                    .addUserMessage("What is the capital of France?")
                    .build());
                System.out.println(message.id());
            } finally {
                client.close();
            }
        }
    }
}
