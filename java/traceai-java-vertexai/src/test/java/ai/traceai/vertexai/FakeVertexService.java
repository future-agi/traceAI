package ai.traceai.vertexai;

import com.google.api.gax.core.NoCredentialsProvider;
import com.google.api.gax.grpc.GrpcTransportChannel;
import com.google.api.gax.rpc.FixedTransportChannelProvider;
import com.google.auth.oauth2.AccessToken;
import com.google.auth.oauth2.GoogleCredentials;
import com.google.cloud.vertexai.VertexAI;
import com.google.cloud.vertexai.api.CountTokensRequest;
import com.google.cloud.vertexai.api.CountTokensResponse;
import com.google.cloud.vertexai.api.GenerateContentRequest;
import com.google.cloud.vertexai.api.GenerateContentResponse;
import com.google.cloud.vertexai.api.LlmUtilityServiceClient;
import com.google.cloud.vertexai.api.LlmUtilityServiceSettings;
import com.google.cloud.vertexai.api.PredictionServiceClient;
import com.google.cloud.vertexai.api.PredictionServiceSettings;
import io.grpc.ManagedChannel;
import io.grpc.MethodDescriptor;
import io.grpc.Server;
import io.grpc.ServerServiceDefinition;
import io.grpc.Status;
import io.grpc.inprocess.InProcessChannelBuilder;
import io.grpc.inprocess.InProcessServerBuilder;
import io.grpc.protobuf.ProtoUtils;
import io.grpc.stub.ServerCalls;
import io.grpc.stub.StreamObserver;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * An in-process Vertex AI gRPC service. The released SDK talks to it through its
 * own clients, so tests exercise the real request and stream code without network.
 */
final class FakeVertexService implements AutoCloseable {
    private static final MethodDescriptor<GenerateContentRequest, GenerateContentResponse> GENERATE =
        MethodDescriptor.<GenerateContentRequest, GenerateContentResponse>newBuilder()
            .setType(MethodDescriptor.MethodType.UNARY)
            .setFullMethodName("google.cloud.aiplatform.v1.PredictionService/GenerateContent")
            .setRequestMarshaller(ProtoUtils.marshaller(GenerateContentRequest.getDefaultInstance()))
            .setResponseMarshaller(ProtoUtils.marshaller(GenerateContentResponse.getDefaultInstance()))
            .build();
    private static final MethodDescriptor<GenerateContentRequest, GenerateContentResponse> STREAM =
        MethodDescriptor.<GenerateContentRequest, GenerateContentResponse>newBuilder()
            .setType(MethodDescriptor.MethodType.SERVER_STREAMING)
            .setFullMethodName("google.cloud.aiplatform.v1.PredictionService/StreamGenerateContent")
            .setRequestMarshaller(ProtoUtils.marshaller(GenerateContentRequest.getDefaultInstance()))
            .setResponseMarshaller(ProtoUtils.marshaller(GenerateContentResponse.getDefaultInstance()))
            .build();
    private static final MethodDescriptor<CountTokensRequest, CountTokensResponse> COUNT =
        MethodDescriptor.<CountTokensRequest, CountTokensResponse>newBuilder()
            .setType(MethodDescriptor.MethodType.UNARY)
            .setFullMethodName("google.cloud.aiplatform.v1.LlmUtilityService/CountTokens")
            .setRequestMarshaller(ProtoUtils.marshaller(CountTokensRequest.getDefaultInstance()))
            .setResponseMarshaller(ProtoUtils.marshaller(CountTokensResponse.getDefaultInstance()))
            .build();

    final List<GenerateContentRequest> requests = new CopyOnWriteArrayList<>();
    volatile GenerateContentResponse unaryResponse = GenerateContentResponse.getDefaultInstance();
    volatile Status unaryError;
    volatile List<GenerateContentResponse> streamChunks = new ArrayList<>();
    volatile Status streamError;
    volatile CountTokensResponse countResponse = CountTokensResponse.getDefaultInstance();

    private final Server server;
    private final ManagedChannel channel;
    final VertexAI vertexAI;

    FakeVertexService() throws IOException {
        String name = "vertex-" + UUID.randomUUID();
        ServerServiceDefinition prediction = ServerServiceDefinition
            .builder("google.cloud.aiplatform.v1.PredictionService")
            .addMethod(GENERATE, ServerCalls.asyncUnaryCall(this::generate))
            .addMethod(STREAM, ServerCalls.asyncServerStreamingCall(this::stream))
            .build();
        ServerServiceDefinition utility = ServerServiceDefinition
            .builder("google.cloud.aiplatform.v1.LlmUtilityService")
            .addMethod(COUNT, ServerCalls.asyncUnaryCall(this::count))
            .build();
        server = InProcessServerBuilder.forName(name).directExecutor()
            .addService(prediction).addService(utility).build().start();
        channel = InProcessChannelBuilder.forName(name).directExecutor().build();
        FixedTransportChannelProvider transport =
            FixedTransportChannelProvider.create(GrpcTransportChannel.create(channel));
        vertexAI = new VertexAI.Builder()
            .setProjectId("synthetic-project")
            .setLocation("us-central1")
            .setCredentials(GoogleCredentials.create(new AccessToken("synthetic-token", null)))
            .setPredictionClientSupplier(() -> {
                try {
                    return PredictionServiceClient.create(PredictionServiceSettings.newBuilder()
                        .setCredentialsProvider(NoCredentialsProvider.create())
                        .setTransportChannelProvider(transport)
                        .build());
                } catch (IOException e) {
                    throw new IllegalStateException(e);
                }
            })
            .setLlmClientSupplier(() -> {
                try {
                    return LlmUtilityServiceClient.create(LlmUtilityServiceSettings.newBuilder()
                        .setCredentialsProvider(NoCredentialsProvider.create())
                        .setTransportChannelProvider(transport)
                        .build());
                } catch (IOException e) {
                    throw new IllegalStateException(e);
                }
            })
            .build();
    }

    private void generate(GenerateContentRequest request, StreamObserver<GenerateContentResponse> observer) {
        requests.add(request);
        if (unaryError != null) {
            observer.onError(unaryError.asRuntimeException());
            return;
        }
        observer.onNext(unaryResponse);
        observer.onCompleted();
    }

    private void stream(GenerateContentRequest request, StreamObserver<GenerateContentResponse> observer) {
        requests.add(request);
        for (GenerateContentResponse chunk : streamChunks) {
            observer.onNext(chunk);
        }
        if (streamError != null) {
            observer.onError(streamError.asRuntimeException());
        } else {
            observer.onCompleted();
        }
    }

    private void count(CountTokensRequest request, StreamObserver<CountTokensResponse> observer) {
        observer.onNext(countResponse);
        observer.onCompleted();
    }

    @Override
    public void close() {
        vertexAI.close();
        channel.shutdownNow();
        server.shutdownNow();
    }
}
