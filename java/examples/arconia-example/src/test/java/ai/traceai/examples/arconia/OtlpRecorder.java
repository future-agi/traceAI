package ai.traceai.examples.arconia;

import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.CopyOnWriteArrayList;

/** A stand-in OTLP/HTTP collector. It keeps the exact bytes, headers and path of every POST. */
final class OtlpRecorder implements AutoCloseable {

    record Post(String path, Map<String, String> headers, byte[] body) {
        ExportTraceServiceRequest decode() throws IOException {
            return ExportTraceServiceRequest.parseFrom(body);
        }
    }

    private final HttpServer server;
    private final List<Post> posts = new CopyOnWriteArrayList<>();

    OtlpRecorder() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            Map<String, String> headers = new TreeMap<>(String.CASE_INSENSITIVE_ORDER);
            exchange.getRequestHeaders().forEach((k, v) -> headers.put(k, String.join(",", v)));
            posts.add(new Post(exchange.getRequestURI().getPath(), headers, exchange.getRequestBody().readAllBytes()));
            exchange.getResponseHeaders().add("Content-Type", "application/x-protobuf");
            exchange.sendResponseHeaders(200, -1);
            exchange.close();
        });
        server.start();
    }

    String baseUrl() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    List<Post> posts() {
        return new ArrayList<>(posts);
    }

    @Override
    public void close() {
        server.stop(0);
    }
}
