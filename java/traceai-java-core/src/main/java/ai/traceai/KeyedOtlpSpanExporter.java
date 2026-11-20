package ai.traceai;

import io.opentelemetry.exporter.internal.otlp.traces.TraceRequestMarshaler;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * OTLP/HTTP protobuf exporter for the FI key pair. The target was approved by {@link KeyTransport}, the
 * client never follows a redirect and, for plaintext, uses no proxy.
 */
final class KeyedOtlpSpanExporter implements SpanExporter {
    private static final Logger logger = LoggerFactory.getLogger(KeyedOtlpSpanExporter.class);
    private static final Duration TIMEOUT = Duration.ofSeconds(30);
    private final URI endpoint;
    private final HttpClient client;
    private final Map<String, String> headers = new LinkedHashMap<>();

    KeyedOtlpSpanExporter(KeyTransport.Target target, String apiKey, String secretKey, String projectName) {
        this.endpoint = target.endpoint("/tracer/v1/traces");
        this.client = KeyTransport.client(target, TIMEOUT);
        headers.put("X-Api-Key", apiKey);
        headers.put("X-Secret-Key", secretKey);
        headers.put("fi-project-name", projectName);
    }

    @Override public CompletableResultCode export(Collection<SpanData> spans) {
        CompletableResultCode result = new CompletableResultCode();
        try {
            ByteArrayOutputStream body = new ByteArrayOutputStream();
            TraceRequestMarshaler.create(spans).writeBinaryTo(body);
            HttpRequest.Builder request = HttpRequest.newBuilder(endpoint).timeout(TIMEOUT)
                .header("Content-Type", "application/x-protobuf").POST(HttpRequest.BodyPublishers.ofByteArray(body.toByteArray()));
            headers.forEach(request::header);
            client.sendAsync(request.build(), HttpResponse.BodyHandlers.discarding()).whenComplete((response, failure) -> {
                if (failure != null) { logger.warn("TraceAI export failed"); result.fail(); return; }
                int status = response.statusCode();
                if (status >= 300 && status < 400) { logger.warn("TraceAI export refused a redirect"); result.fail(); }
                else if (status >= 200 && status < 300) result.succeed();
                else { logger.warn("TraceAI export rejected (HTTP {})", status); result.fail(); }
            });
        } catch (Throwable failure) {
            logger.warn("TraceAI export failed");
            result.fail();
        }
        return result;
    }

    @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
    @Override public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
}
