package ai.traceai.auto;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.concurrent.CompletableFuture;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/**
 * One chat call through an OkHttpClient the application builds itself, with no agent API calls.
 * The build compiles this against its own OkHttp; a child JVM then runs it on whichever OkHttp,
 * Okio and Kotlin jars the test puts on the classpath. It therefore uses only calls whose JVM
 * signature is the same in OkHttp 3.14, 4.x and 5.x, and it prints the jar each library came from.
 */
public final class OkHttpVersionApp {
    public static void main(String[] args) throws Exception {
        System.out.println("OKHTTP_JAR=" + jar("okhttp3.OkHttpClient"));
        System.out.println("OKIO_JAR=" + jar("okio.Buffer"));
        System.out.println("KOTLIN_JAR=" + jar("kotlin.Unit"));
        System.out.println("OKHTTP_VERSION=" + version());
        byte[] outgoing = PlainApp.REQUEST.getBytes(StandardCharsets.UTF_8);
        RequestBody body = new RequestBody() {
            @Override public MediaType contentType() { return MediaType.parse("application/json"); }
            @Override public long contentLength() { return outgoing.length; }
            @Override public void writeTo(okio.BufferedSink sink) throws IOException { sink.write(outgoing); }
        };
        OkHttpClient client = new OkHttpClient.Builder().build();
        try {
            Request request = new Request.Builder().url(args[1]).post(body).build();
            byte[] received;
            if (args[2].endsWith("async")) {
                CompletableFuture<byte[]> pending = new CompletableFuture<>();
                client.newCall(request).enqueue(new Callback() {
                    @Override public void onFailure(Call call, IOException error) { pending.completeExceptionally(error); }
                    @Override public void onResponse(Call call, Response response) {
                        try (Response closing = response) { pending.complete(copy(closing.body().byteStream())); }
                        catch (Throwable error) { pending.completeExceptionally(error); }
                    }
                });
                received = pending.join();
            } else try (Response response = client.newCall(request).execute()) {
                received = copy(response.body().byteStream());
            }
            System.out.println("BODY=" + Base64.getEncoder().encodeToString(received));
        } finally { client.dispatcher().executorService().shutdown(); client.connectionPool().evictAll(); }
    }
    /** File name of the jar a class was loaded from, or "none" when the class is not on the classpath. */
    static String jar(String type) {
        try {
            String location = Class.forName(type, false, OkHttpVersionApp.class.getClassLoader()).getProtectionDomain().getCodeSource().getLocation().getPath();
            return location.substring(location.lastIndexOf('/') + 1);
        } catch (Throwable absent) { return "none"; }
    }
    /** OkHttp 4 and later publish their version as a constant; OkHttp 3 has no such class. */
    static String version() {
        try { return String.valueOf(Class.forName("okhttp3.OkHttp").getField("VERSION").get(null)); }
        catch (Throwable absent) { return "none"; }
    }
    static byte[] copy(InputStream stream) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream(); byte[] chunk = new byte[3];
        int n; while ((n = stream.read(chunk)) >= 0) bytes.write(chunk, 0, n); return bytes.toByteArray();
    }
    private OkHttpVersionApp() {}
}
