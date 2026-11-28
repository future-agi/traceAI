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
    private final java.util.concurrent.ExecutorService executor = KeyTransport.newExecutor();
    private final KeyTransport.Sends sends = new KeyTransport.Sends();
    private final Duration waitLimit;
    private final Map<String, String> headers = new LinkedHashMap<>();

    KeyedOtlpSpanExporter(KeyTransport.Target target, String apiKey, String secretKey, String projectName) {
        this(target, apiKey, secretKey, projectName, Duration.ofSeconds(10));
    }

    KeyedOtlpSpanExporter(KeyTransport.Target target, String apiKey, String secretKey, String projectName, Duration waitLimit) {
        this.endpoint = target.endpoint("/tracer/v1/traces");
        this.waitLimit = waitLimit;
        this.client = KeyTransport.client(target, TIMEOUT, executor);
        headers.put("X-Api-Key", apiKey);
        headers.put("X-Secret-Key", secretKey);
        headers.put("fi-project-name", projectName);
    }

    @Override public CompletableResultCode export(Collection<SpanData> spans) {
        CompletableResultCode result = new CompletableResultCode();
        // A failed batch is dropped: it is never retried, and never sent to a different target.
        if (!sends.begin()) { result.fail(); return result; }
        try {
            ByteArrayOutputStream body = new ByteArrayOutputStream();
            TraceRequestMarshaler.create(spans).writeBinaryTo(body);
            HttpRequest.Builder request = HttpRequest.newBuilder(endpoint).timeout(TIMEOUT)
                .header("Content-Type", "application/x-protobuf").POST(HttpRequest.BodyPublishers.ofByteArray(body.toByteArray()));
            headers.forEach(request::header);
            client.sendAsync(request.build(), HttpResponse.BodyHandlers.discarding()).whenComplete((response, failure) -> {
                // The send ends before its result completes: a callback of the result may call shutdown() and must not wait for this send.
                boolean ok = false;
                try {
                    if (failure != null) logger.warn("TraceAI export failed");
                    else {
                        int status = response.statusCode();
                        try { KeyTransport.checkStatus(status); ok = status >= 200 && status < 300; if (!ok) logger.warn("TraceAI export rejected (HTTP {})", status); }
                        catch (KeyTransport.Refused redirect) { logger.warn("TraceAI export refused a redirect"); }
                    }
                } finally { sends.end(); }
                if (ok) result.succeed(); else result.fail();
            });
        } catch (Throwable failure) {
            sends.end();
            logger.warn("TraceAI export failed");
            result.fail();
        }
        return result;
    }

    /** Succeeds only when no send is in flight after the wait limit, and the exporter is still open. */
    @Override public CompletableResultCode flush() {
        if (sends.closed()) return CompletableResultCode.ofFailure();
        return sends.awaitIdle(waitLimit) ? CompletableResultCode.ofSuccess() : CompletableResultCode.ofFailure();
    }

    /** Refuses further exports, waits for a send in flight up to the wait limit (failure when one still runs), then releases the client. */
    @Override public CompletableResultCode shutdown() {
        sends.close();
        boolean idle = sends.awaitIdle(waitLimit);
        KeyTransport.release(client, executor);
        return idle ? CompletableResultCode.ofSuccess() : CompletableResultCode.ofFailure();
    }
}
