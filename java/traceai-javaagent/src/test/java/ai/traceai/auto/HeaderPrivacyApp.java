package ai.traceai.auto;

import io.opentelemetry.api.common.*;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** Credentials from an uninstrumented transport must still be removed from forwarded spans. */
public final class HeaderPrivacyApp {
    static final Map<String, String> HEADERS = Map.of(
        "Authorization", "Bearer AUTHORIZATION_PRIVATE_HEADER",
        "x-api-key", "X_API_PRIVATE_HEADER", "api-key", "API_PRIVATE_HEADER", "xi-api-key", "XI_API_PRIVATE_HEADER",
        "x-goog-api-key", "GOOGLE_PRIVATE_HEADER", "anthropic-api-key", "ANTHROPIC_PRIVATE_HEADER",
        "Ocp-Apim-Subscription-Key", "AZURE_PRIVATE_HEADER", "X-Amz-Security-Token", "AWS_SESSION_PRIVATE_HEADER");
    static Attributes attributes(boolean arrays) {
        var builder = Attributes.builder();
        HEADERS.forEach((header, value) -> {
            String name = "http.request.header." + header.toLowerCase(Locale.ROOT);
            if (arrays) builder.put(AttributeKey.stringArrayKey(name), List.of(value, "second-private-value"));
            else builder.put(name, value);
        });
        return builder.build();
    }
    static void run(String[] args) throws Exception {
        var memory = InMemorySpanExporter.create();
        var attributes = attributes(args[2].endsWith("arrays"));
        var resource = Resource.create(attributes.toBuilder().put("service.name", "header-privacy-service").build());
        try (var provider = SdkTracerProvider.builder().setResource(resource).addSpanProcessor(SimpleSpanProcessor.create(memory)).build()) {
            var link = SpanContext.create("12345678901234567890123456789012", "1234567890123456", TraceFlags.getSampled(), TraceState.getDefault());
            var span = provider.tracerBuilder("header-privacy-scope").setInstrumentationVersion("1.0").build()
                .spanBuilder("forwarded-header-privacy").addLink(link, attributes).setAllAttributes(attributes).startSpan();
            HttpURLConnection connection = (HttpURLConnection)URI.create(args[1]).toURL().openConnection();
            try {
                connection.setConnectTimeout(5000); connection.setReadTimeout(5000);
                connection.setRequestMethod("POST"); connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json"); HEADERS.forEach(connection::setRequestProperty);
                try (var output = connection.getOutputStream()) { output.write(PlainApp.request(args[2]).getBytes(StandardCharsets.UTF_8)); }
                int status = connection.getResponseCode();
                if (status != 200) throw new AssertionError("Unexpected request status: " + status);
                try (var input = connection.getInputStream()) { System.out.println("BODY=" + Base64.getEncoder().encodeToString(input.readAllBytes())); }
                System.out.println("REAL_HEADER_REQUEST_STATUS=" + status);
            } finally { connection.disconnect(); span.addEvent("request-headers", attributes); span.end(); }
            var original = memory.getFinishedSpanItems().get(0);
            if (!attributes.asMap().equals(original.getAttributes().asMap()) || !attributes.asMap().equals(original.getEvents().get(0).getAttributes().asMap())
                || !attributes.asMap().equals(original.getLinks().get(0).getAttributes().asMap()) || !resource.equals(original.getResource())) {
                throw new AssertionError("Agent mutated the application's credential attributes");
            }
            System.out.println("APP_HEADERS_UNCHANGED=true");
        }
    }
}
