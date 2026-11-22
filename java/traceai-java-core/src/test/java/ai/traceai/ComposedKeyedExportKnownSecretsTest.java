package ai.traceai;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The composed export path: the known-secret replacement runs before the keyed send. A raw socket
 * receives what TraceAI.init exports to a plaintext loopback target and the request body is checked byte for byte.
 */
class ComposedKeyedExportKnownSecretsTest {
    private static final String API_KEY = "PFX-composed-api-key-0001";
    private static final String SECRET_KEY = "PFX-composed-secret-key-0002";
    private ServerSocket sink;
    private Thread acceptor;
    private final List<byte[]> bodies = new CopyOnWriteArrayList<>();
    private final List<String> heads = new CopyOnWriteArrayList<>();

    @BeforeEach void start() throws IOException {
        ai.traceai.privacy.KnownSecretsTestReset.reset();
        TraceAI.shutdown(); GlobalOpenTelemetry.resetForTest(); KeyTransport.resetWarningForTest();
        sink = new ServerSocket(0, 10, InetAddress.getByName("127.0.0.1"));
        acceptor = new Thread(() -> {
            while (!sink.isClosed()) {
                try (Socket socket = sink.accept()) { serve(socket); } catch (IOException ignored) { }
            }
        }, "composed-sink");
        acceptor.setDaemon(true); acceptor.start();
    }
    @AfterEach void stop() throws IOException {
        TraceAI.shutdown(); GlobalOpenTelemetry.resetForTest(); KeyTransport.resetWarningForTest();
        ai.traceai.privacy.KnownSecretsTestReset.reset();
        sink.close();
    }

    private void serve(Socket socket) throws IOException {
        socket.setSoTimeout(5000);
        InputStream in = socket.getInputStream();
        ByteArrayOutputStream head = new ByteArrayOutputStream();
        int match = 0;
        while (match < 4) {
            int b = in.read();
            if (b < 0) return;
            head.write(b);
            match = (b == '\r' && (match == 0 || match == 2)) || (b == '\n' && (match == 1 || match == 3)) ? match + 1 : (b == '\r' ? 1 : 0);
        }
        String text = head.toString(StandardCharsets.ISO_8859_1);
        int length = 0;
        for (String line : text.split("\r\n")) if (line.toLowerCase().startsWith("content-length:")) length = Integer.parseInt(line.substring(15).trim());
        byte[] body = in.readNBytes(length);
        heads.add(text); bodies.add(body);
        OutputStream out = socket.getOutputStream();
        out.write("HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
        out.flush();
    }

    @Test void aKnownSecretIsAbsentFromWhatTheKeyedExportSends() throws Exception {
        TraceAI.init(TraceConfig.builder().baseUrl("http://127.0.0.1:" + sink.getLocalPort())
            .apiKey(API_KEY).secretKey(SECRET_KEY).projectName("p").exportIntervalMs(60000).build());
        FITracer tracer = TraceAI.getTracer();
        Span span = tracer.startSpan("span " + SECRET_KEY, FISpanKind.LLM);
        tracer.setAttribute(span, "custom.attribute", "value " + SECRET_KEY + " end");
        span.addEvent("event " + SECRET_KEY, Attributes.builder().put("event.attribute", "e " + SECRET_KEY).build());
        span.end();
        TraceAI.shutdown();

        assertThat(bodies).as("the sink received an export").isNotEmpty();
        // The credentials travel in the request headers by design; only the payload is screened.
        assertThat(heads.get(0).toLowerCase()).contains("x-secret-key");
        String wire = new String(bodies.get(0), StandardCharsets.ISO_8859_1);
        assertThat(wire).contains("custom.attribute").contains("event.attribute").contains("<redacted>");
        assertThat(wire).doesNotContain("composed-secret-key").doesNotContain("PFX-");
    }
}
