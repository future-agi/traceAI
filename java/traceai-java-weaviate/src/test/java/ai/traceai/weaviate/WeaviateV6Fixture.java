package ai.traceai.weaviate;

import com.sun.net.httpserver.HttpServer;
import io.grpc.Server;
import io.grpc.Status;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;
import io.grpc.stub.StreamObserver;
import io.weaviate.client6.v1.api.WeaviateClient;
import io.weaviate.client6.v1.api.WeaviateClientAsync;
import io.weaviate.client6.v1.internal.grpc.protocol.WeaviateGrpc;
import io.weaviate.client6.v1.internal.grpc.protocol.WeaviateProtoBatch;
import io.weaviate.client6.v1.internal.grpc.protocol.WeaviateProtoBatchDelete;
import io.weaviate.client6.v1.internal.grpc.protocol.WeaviateProtoProperties;
import io.weaviate.client6.v1.internal.grpc.protocol.WeaviateProtoSearchGet;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;

/** Real SDK connections; only the remote server is replaced. No database or credentials. */
final class WeaviateV6Fixture implements AutoCloseable {
    static final String UUID = "00000000-0000-0000-0000-000000000001";
    static final String PRIVATE = "synthetic-private-document";
    final List<WeaviateProtoSearchGet.SearchRequest> searches = new CopyOnWriteArrayList<>();
    final List<String> restRequests = new CopyOnWriteArrayList<>();
    volatile boolean failSearch;
    volatile boolean emptySearch;
    volatile boolean failRest;
    volatile boolean partialBatch;
    volatile boolean holdSearch;
    volatile StreamObserver<WeaviateProtoSearchGet.SearchReply> pendingSearch;
    private final HttpServer http;
    private final Server grpc;

    WeaviateV6Fixture() throws Exception {
        http = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        http.createContext("/", exchange -> {
            String path = exchange.getRequestURI().getPath();
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            restRequests.add(exchange.getRequestMethod() + " " + exchange.getRequestURI() + " " + body);
            String response = "{}";
            int status = 200;
            if (path.endsWith("/meta")) {
                response = "{\"version\":\"1.32.0\",\"modules\":{}}";
            } else if (path.contains("/objects")) {
                if (failRest) {
                    status = 422;
                    response = "{\"error\":[{\"message\":\"" + PRIVATE + "\"}]}";
                } else if (exchange.getRequestMethod().equals("DELETE")) {
                    status = 204;
                    response = "";
                } else {
                    response = "{\"id\":\"" + UUID + "\",\"class\":\"Article\",\"properties\":{\"title\":\"" + PRIVATE + "\"}}";
                }
            }
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(status, status == 204 ? -1 : bytes.length);
            if (status != 204) exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        http.start();
        grpc = NettyServerBuilder.forAddress(new InetSocketAddress("127.0.0.1", 0))
                .addService(new WeaviateGrpc.WeaviateImplBase() {
                    @Override public void search(WeaviateProtoSearchGet.SearchRequest request,
                                                  StreamObserver<WeaviateProtoSearchGet.SearchReply> observer) {
                        searches.add(request);
                        if (holdSearch) {
                            pendingSearch = observer;
                        } else if (failSearch) {
                            observer.onError(Status.INVALID_ARGUMENT.withDescription(PRIVATE).asRuntimeException());
                        } else {
                            observer.onNext(searchReply(emptySearch));
                            observer.onCompleted();
                        }
                    }
                    @Override public void batchObjects(WeaviateProtoBatch.BatchObjectsRequest request,
                                                        StreamObserver<WeaviateProtoBatch.BatchObjectsReply> observer) {
                        var reply = WeaviateProtoBatch.BatchObjectsReply.newBuilder();
                        if (partialBatch) {
                            reply.addErrors(WeaviateProtoBatch.BatchObjectsReply.BatchError.newBuilder()
                                    .setIndex(0).setError(PRIVATE));
                        }
                        observer.onNext(reply.build());
                        observer.onCompleted();
                    }
                    @Override public void batchDelete(WeaviateProtoBatchDelete.BatchDeleteRequest request,
                                                       StreamObserver<WeaviateProtoBatchDelete.BatchDeleteReply> observer) {
                        observer.onNext(WeaviateProtoBatchDelete.BatchDeleteReply.newBuilder()
                                .setMatches(2).setSuccessful(partialBatch ? 1 : 2)
                                .setFailed(partialBatch ? 1 : 0).build());
                        observer.onCompleted();
                    }
                }).build().start();
    }

    static WeaviateProtoSearchGet.SearchReply searchReply(boolean empty) {
        var reply = WeaviateProtoSearchGet.SearchReply.newBuilder();
        if (!empty) {
            reply.addResults(WeaviateProtoSearchGet.SearchResult.newBuilder()
                    .setProperties(WeaviateProtoSearchGet.PropertiesResult.newBuilder()
                            .setNonRefProps(WeaviateProtoProperties.Properties.newBuilder()
                                    .putFields("title", WeaviateProtoProperties.Value.newBuilder()
                                            .setTextValue(PRIVATE).build())))
                    .setMetadata(WeaviateProtoSearchGet.MetadataResult.newBuilder()
                            .setId(UUID).setDistancePresent(true).setDistance(0.25f)));
        }
        return reply.build();
    }

    int httpPort() { return http.getAddress().getPort(); }
    int grpcPort() { return grpc.getPort(); }

    WeaviateClient client() {
        return WeaviateClient.connectToLocal(c -> c.host("127.0.0.1").port(http.getAddress().getPort()).grpcPort(grpc.getPort()));
    }

    WeaviateClientAsync asyncClient() {
        return WeaviateClientAsync.connectToLocal(c -> c.host("127.0.0.1").port(http.getAddress().getPort()).grpcPort(grpc.getPort()));
    }

    @Override public void close() throws Exception {
        grpc.shutdownNow().awaitTermination(5, TimeUnit.SECONDS);
        http.stop(0);
    }
}
